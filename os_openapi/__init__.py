"""
    os_openapi
    ---------------------

    The OpenAPI spec renderer for Sphinx. It's a new way to document your
    RESTful API. Based on ``sphinxcontrib-openapi``.

"""

import functools
import itertools
import os

import yaml
import jsonschema
import collections
import collections.abc

from sphinx.util import logging
import pbr.version

from docutils import nodes

from docutils.parsers.rst import Directive
from docutils.parsers.rst import directives
from sphinx.util.docutils import SphinxDirective
from sphinx.util.osutil import copyfile

# from sphinx_mdinclude import convert as mdconvert
from markdown import markdown

__version__ = pbr.version.VersionInfo("os_openapi").version_string()

LOG = logging.getLogger(__name__)


# Locally cache spec to speedup processing of same spec file in multiple
# openapi directives
@functools.lru_cache()
def _get_spec(abspath, encoding):
    with open(abspath, "rt", encoding=encoding) as stream:
        return yaml.safe_load(stream)


class openapi(nodes.Part, nodes.Element):
    """OpenAPI node"""

    pass


class openapi_operation_header(nodes.Part, nodes.Element):
    """Operation Header node"""

    pass


class OpenApiRefResolver(jsonschema.RefResolver):
    """
    Overrides resolve_remote to support both YAML and JSON
    OpenAPI schemas.
    """

    try:
        import requests

        _requests = requests
    except ImportError:
        _requests = None

    def resolve_remote(self, uri):
        scheme, _, path, _, _ = urlsplit(uri)
        _, extension = os.path.splitext(path)

        if extension not in [".yml", ".yaml"] or scheme in self.handlers:
            return super(OpenApiRefResolver, self).resolve_remote(uri)

        if scheme in ["http", "https"] and self._requests:
            response = self._requests.get(uri)
            result = yaml.safe_load(response.content)
        else:
            # Otherwise, pass off to urllib and assume utf-8
            with closing(urlopen(uri)) as url:
                response = url.read().decode("utf-8")
                result = yaml.safe_load(response)

        if self.cache_remote:
            self.store[uri] = result
        return result


def _resolve_refs(uri, spec):
    """Resolve JSON references in a given dictionary.

    OpenAPI spec may contain JSON references to its nodes or external
    sources, so any attempt to rely that there's some expected attribute
    in the spec may fail. So we need to resolve JSON references before
    we use it (i.e. replace with referenced object). For details see:

        https://tools.ietf.org/html/draft-pbryan-zyp-json-ref-02

    The input spec is modified in-place despite being returned from
    the function.
    """

    resolver = OpenApiRefResolver(uri, spec)

    def _do_resolve(node, seen=[]):
        if isinstance(node, collections.abc.Mapping) and "$ref" in node:
            ref = node["$ref"]
            with resolver.resolving(ref) as resolved:
                if ref in seen:
                    return {
                        type: "object"
                    }  # return a distinct object for recursive data type
                return _do_resolve(
                    resolved, seen + [ref]
                )  # might have other references
        elif isinstance(node, collections.abc.Mapping):
            for k, v in node.items():
                node[k] = _do_resolve(v, seen)
        elif isinstance(node, (list, tuple)):
            for i in range(len(node)):
                node[i] = _do_resolve(node[i], seen)
        return node

    return _do_resolve(spec)


def normalize_spec(spec, **options):
    # OpenAPI spec may contain JSON references, so we need resolve them
    # before we access the actual values trying to build an httpdomain
    # markup. Since JSON references may be relative, it's crucial to
    # pass a document URI in order to properly resolve them.
    spec = _resolve_refs(options.get("uri", ""), spec)

    # OpenAPI spec may contain common endpoint's parameters top-level.
    # In order to do not place if-s around the code to handle special
    # cases, let's normalize the spec and push common parameters inside
    # endpoints definitions.
    for endpoint in spec.get("paths", {}).values():
        parameters = endpoint.pop("parameters", [])
        for method in endpoint.values():
            method.setdefault("parameters", [])
            method["parameters"].extend(parameters)


class OpenApiDirective(SphinxDirective):
    """Directive implementation"""

    required_arguments = 1
    option_spec = dict(
        {
            "encoding": directives.encoding,  # useful for non-ascii cases :)
        },
    )

    def run(self):
        # target = nodes.target()
        relpath, abspath = self.env.relfn2path(
            directives.path(self.arguments[0])
        )

        # URI parameter is crucial for resolving relative references. So we
        # need to set this option properly as it's used later down the
        # stack.
        self.options.setdefault("uri", "file://%s" % abspath)

        # Add a given OpenAPI spec as a dependency of the referring
        # reStructuredText document, so the document is rebuilt each time
        # the spec is changed.
        self.env.note_dependency(relpath)

        # Read the spec using encoding passed to the directive or fallback to
        # the one specified in Sphinx's config.
        encoding = self.options.get("encoding", self.config.source_encoding)

        spec = _get_spec(abspath, encoding)

        normalize_spec(spec)

        results = []

        groups = nodes.target("", "", ids=["openapi-groups"])
        results.append(self._get_spec_header_nodes(spec))
        for tag in spec.get("tags", ["default"]):
            results.append(self._get_api_group_nodes(spec, tag))

        # self.state.nested_parse(self.state, self.content_offset, results)

        return results

    def _get_spec_header_nodes(self, spec):
        description = spec["info"].get("description")
        summary = spec["info"].get("summary")
        header = nodes.paragraph(text=description if description else summary)
        return header

    def _get_api_group_nodes(self, spec, tag):
        tag_name = tag["name"]
        targetid = f"group-{tag_name}"
        target_node = nodes.target("", "", ids=[targetid])
        section = nodes.section(
            classes=["api-group", "accordion"], ids=[targetid]
        )
        section += nodes.title(text=tag_name)
        group_descr = tag.get("description", "")
        if group_descr:
            section += nodes.paragraph(text=group_descr)

        for url, path_def in spec["paths"].items():
            for method in ["head", "get", "post", "put", "delete"]:
                if method in path_def and tag_name in path_def[method].get(
                    "tags"
                ):
                    operation_def = path_def[method]
                    for child in self._get_operation_nodes(
                        spec, url, method, operation_def
                    ):
                        section += child

        return section

    def _get_operation_nodes(self, spec, path, method, operation_spec):
        # We might want to have multiple separate entries for single url (a.k.a. actions)
        operation_specs = []
        if not path.endswith("/action"):
            operation_specs.append((operation_spec, None, None))
        else:
            # Body
            body = (
                operation_spec.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            if body:
                actions = body.get("oneOf", [])
                discriminator = body.get("x-openstack", {}).get(
                    "discriminator"
                )
                if actions and discriminator == "action":
                    for candidate in actions:
                        action_name = candidate.get("x-openstack", {}).get(
                            "action-name"
                        )
                        if not action_name:
                            # No action name on the body. Take 1st property name
                            action_name = list(candidate["properties"].keys())[
                                0
                            ]
                        operation_specs.append(
                            (operation_spec, action_name, candidate)
                        )
                else:
                    # This does not look like an action, just return operation
                    operation_specs.append((operation_spec, None, None))
            else:
                # This does not look like an action (no body), just return operation
                operation_specs.append((operation_spec, None, None))

        for operation_spec, action_name, action_body in operation_specs:
            # Iterate over spec and eventual actions
            op_id = (
                operation_spec["operationId"]
                .replace(":", "_")
                .replace("/", "_")
            )
            if action_name:
                op_id += f"-{action_name}"

            container = nodes.section(
                ids=[f"operation-{op_id}"],
                classes=[
                    "accordion-item",
                    "operation",
                    "operation-" + method,
                    "gy-2",
                ],
            )
            # container += nodes.title(text=operation_spec.get("summary", path), classes=["invisible"])
            op_header = openapi_operation_header()
            if not action_name:
                op_header["summary"] = operation_spec.get("summary")
            else:
                op_header["summary"] = action_body.get(
                    "description", f"{action_name} action"
                )
            # op_header["description"] = operation_spec.get("description")
            op_header["operationId"] = op_id
            op_header["method"] = method
            op_header["path"] = path
            container += op_header
            content = nodes.compound(
                classes=["accordion-collapse collapse accordion-body"],
                ids=[f"collapse{op_id}"],
            )
            content += nodes.paragraph(
                text=operation_spec.get("description", ""),
                classes=["accordion-body"],
            )

            content += self._get_operation_request_node(
                op_id, operation_spec, action_name, action_body
            )
            content += self._get_operation_response_node(
                op_id, operation_spec, action_name
            )

            container += content
            yield container

    def _get_operation_request_node(
        self, operationId, operation_spec, action_name=None, action_body=None
    ):
        """Build the Request section"""
        request = nodes.section(ids=[f"api-req-{operationId}"])
        request += nodes.title(text="Request")

        table = nodes.table()
        tgroup = nodes.tgroup(cols=4)
        for _ in range(4):
            colspec = nodes.colspec(colwidth=1)
            tgroup += colspec
        table += tgroup
        # Build table headers
        thead = nodes.thead()
        tgroup += thead
        tr = nodes.row()
        thead += tr
        for col in ["Name", "Location", "Type", "Description"]:
            tr += nodes.entry("", nodes.paragraph(text=col))
        # Table data
        rows = []
        # Parameters
        for param in operation_spec.get("parameters", []):
            rows.append(self._get_request_table_param_row(param))
        # Body
        if not action_body:
            body = (
                operation_spec.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
        else:
            body = action_body

        for el in self._get_request_table_field_row(body, None, set()):
            rows.append(el)

        tbody = nodes.tbody()
        tbody.extend(rows)
        tgroup += tbody

        if body:
            for key, sample in body.get("examples", {}).items():
                for el in self._get_body_examples(key, sample):
                    response += el

        if rows:
            request += table

        return request

    def _get_operation_response_node(
        self, operationId, operation_spec, action_name=None
    ):
        """Build the Response section"""
        responses = nodes.section(ids=[f"api-res-{operationId}"])
        responses += nodes.title(text="Responses")

        response_specs = operation_spec.get("responses")
        for code, response_spec in sorted(response_specs.items()):
            rsp_id = "response-%d" % self.env.new_serialno("response")
            response = nodes.section(ids=[rsp_id])
            response += nodes.title(text=code)
            descr = response_spec.get("description")
            if descr:
                response += nodes.paragraph(
                    text=descr, classes=["description"]
                )
            responses += response
            response_schema = None

            table = nodes.table()
            tgroup = nodes.tgroup(cols=4)
            for _ in range(4):
                colspec = nodes.colspec(colwidth=1)
                tgroup += colspec
            table += tgroup
            # Build table headers
            thead = nodes.thead()
            tgroup += thead
            tr = nodes.row()
            thead += tr
            for col in ["Name", "Location", "Type", "Description"]:
                tr += nodes.entry("", nodes.paragraph(text=col))
            # Table data
            rows = []
            # TODO(gtema) Operation may return headers
            # for param in operation_spec.get("parameters", []):
            #     rows.append(self._get_request_table_param_row(param))
            # Body
            if not action_name:
                response_schema = (
                    response_spec.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
            else:
                # Iterate over all available responses to find suitable action response
                candidates = []
                body_candidate = (
                    response_spec.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body_candidate:
                    candidates = body_candidate.get("oneOf", [])
                    if not candidates:
                        candidates.append(body_candidate)

                for candidate in candidates:
                    os_ext = candidate.get("x-openstack", {})
                    rsp_act_name = os_ext.get("action-name")
                    if rsp_act_name == action_name:
                        response_schema = candidate

                # TODO(gtema) how to properly identify response code of the
                # action when it returns no body at all. This info is present
                # on the server side, but is missing in openapi

            if response_schema:
                for el in self._get_request_table_field_row(
                    response_schema, None, set()
                ):
                    rows.append(el)

            tbody = nodes.tbody()
            tbody.extend(rows)
            tgroup += tbody

            if rows:
                response += table

            if response_schema:
                for key, sample in response_schema.get("examples", {}).items():
                    for el in self._get_body_examples(key, sample):
                        response += el

        return responses

    def _get_request_table_param_row(self, param):
        """Build a row of a request parameters table with the parameter/header"""
        tr = nodes.row()
        tr += nodes.entry("", nodes.paragraph(text=param["name"]))
        tr += nodes.entry("", nodes.paragraph(text=param["in"]))
        tr += nodes.entry("", nodes.paragraph(text=param["schema"]["type"]))
        tr += nodes.entry("", nodes.paragraph(text=param.get("description")))

        return tr

    def _get_request_table_field_row(self, field, field_name, emited_fields):
        """Emit Request description table row for the body element"""
        if not field:
            return

        typ = field.get("type")
        note = None
        os_ext = field.get("x-openstack", {})
        rows = []
        if os_ext:
            min_ver = os_ext.get("min-ver")
            max_ver = os_ext.get("max-ver")
            if min_ver:
                note = f"<br/><strong>New in version {min_ver}</strong>"
            if max_ver:
                note = (
                    f"<br/><strong>Available until version {max_ver}</strong>"
                )
        param_descr = f'{field.get("description", "")}{note or ""}'

        if typ == "object" and "properties" in field:
            if field_name and field_name not in emited_fields:
                emited_fields.add(field_name)
                tr = nodes.row()
                tr += nodes.entry("", nodes.paragraph(text=field_name))
                tr += nodes.entry("", nodes.paragraph(text="body"))
                tr += nodes.entry(
                    "", nodes.paragraph(text=field.get("type", ""))
                )
                tr += nodes.entry("", nodes.paragraph(text=param_descr))
                yield tr

            for k, v in field["properties"].items():
                for el in self._get_request_table_field_row(
                    v, f"{field_name}.{k}" if field_name else k, emited_fields
                ):
                    yield el
        elif typ == "array":
            pass
        elif typ:
            if field_name and field_name not in emited_fields:
                emited_fields.add(field_name)
                tr = nodes.row()
                tr += nodes.entry("", nodes.paragraph(text=field_name))
                tr += nodes.entry("", nodes.paragraph(text="body"))
                tr += nodes.entry(
                    "", nodes.paragraph(text=field.get("type", ""))
                )
                tr += nodes.entry("", nodes.paragraph(text=param_descr))
                yield tr
        if not typ and "oneOf" in field:
            opts = field["oneOf"]
            discriminator = field.get("x-openstack", {}).get("discriminator")
            if discriminator == "microversion":
                for opt in opts:
                    for el in self._get_request_table_field_row(
                        opt, field_name, emited_fields
                    ):
                        yield el
            elif discriminator == "action":
                for opt in opts:
                    for el in self._get_request_table_field_row(
                        opt, field_name, emited_fields
                    ):
                        yield el

    def _get_body_examples(self, sample_key, sample):
        p = nodes.paragraph("")
        title = "Example"
        if sample_key:
            title += f" ({sample_key})"
        p.append(nodes.strong(text=title))
        yield p
        pre = nodes.literal_block(
            "", classes=["javascript", "highlight-javascript"]
        )
        pre.append(
            nodes.literal(
                text=sample,
                language="javascript",
                classes=["highlight", "code"],
            )
        )
        # TODO(gtema): how to trigger activation of pygments?
        yield pre


def copy_assets(app, exception):
    assets = ("bootstrap.min.css", "bootstrap.bundle.min.js", "api-ref.css")
    builders = ("html", "readthedocs", "readthedocssinglehtmllocalmedia")
    if app.builder.name not in builders or exception:
        return
    LOG.info("Copying assets: %s", ", ".join(assets))
    for asset in assets:
        dest = os.path.join(app.builder.outdir, "_static", asset)
        source = os.path.abspath(os.path.dirname(__file__))
        copyfile(os.path.join(source, "assets", asset), dest)


def add_assets(app):
    app.add_css_file("bootstrap.min.css")
    app.add_css_file("api-ref.css")
    app.add_js_file("bootstrap.bundle.min.js")


def visit_openapi_operation_header(self, node):
    """Render a bootstrap accordion for the operation header"""
    tag_id = node["operationId"]
    method = node["method"]
    summary = node.get("summary", "")
    path = "/".join(
        [
            f'<span class="path_parameter">{x}</span>' if x[0] == "{" else x
            for x in node["path"].split("/")
            if x
        ]
    )
    if len(path) > 0 and path[0] != "/":
        path = "/" + path
    self.body.append(
        f'<button class="accordion-button" type="button" data-bs-toggle="collapse" data-bs-target="#collapse{tag_id}" aria-expanded="false" aria-controls="collapse{tag_id}">'
    )
    self.body.append('<div class="container">')
    self.body.append('<div class="row">')
    self.body.append(
        f'<div class="col-1"><span class="badge label-{method}">{method.upper()}</span></div>'
    )
    self.body.append(
        f'<div class="col-11"><span class="operation-path">{path}</span><span class="operation-summary">{summary or ""}</span></div>'
    )
    self.body.append("</div>")
    self.body.append("</div>")
    self.body.append(f"</button>")

    raise nodes.SkipNode


def setup(app) -> dict[str, bool]:
    # Add some config options around microversions
    app.add_node(
        openapi_operation_header, html=(visit_openapi_operation_header, None)
    )
    # This specifies all our directives that we're adding
    app.add_directive("openapi", OpenApiDirective)
    # app.add_directive('openapi_group', OpenApiGroupDirective)

    app.connect("builder-inited", add_assets)
    # This copies all the assets (css, js, fonts) over to the build
    # _static directory during final build.
    app.connect("build-finished", copy_assets)

    return {
        "parallel_read_safe": True,
        "parallel_write_safe": True,
        "version": __version__,
    }
