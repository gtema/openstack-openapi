"""
    os_openapi
    ---------------------

    The OpenAPI spec renderer for Sphinx. It's a new way to document your
    RESTful API. Based on ``sphinxcontrib-openapi``.

"""

import functools
import os
import json

from typing import Any

from urllib.parse import urlsplit
from urllib.request import urlopen

from ruamel.yaml import YAML
import jsonschema
import collections
import collections.abc
from contextlib import closing

from sphinx.util import logging
import pbr.version

from docutils import nodes

from docutils.parsers.rst import directives
from sphinx.util.docutils import SphinxDirective
from sphinx.util.osutil import copyfile

from myst_parser.mdit_to_docutils.base import make_document
from myst_parser.parsers.docutils_ import (
    Parser,
)

__version__ = pbr.version.VersionInfo("os_openapi").version_string()

LOG = logging.getLogger(__name__)


# Locally cache spec to speedup processing of same spec file in multiple
# openapi directives
@functools.lru_cache()
def _get_spec(abspath, encoding):
    with open(abspath, "rt", encoding=encoding) as stream:
        # It is important to use ruamel since it goes for YAML1.2 which
        # properly understands quotes for nova boolean enum values
        yaml = YAML(typ="safe")
        return yaml.load(stream)


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
            yaml = YAML()
            result = yaml.safe_load(response.content)
        else:
            # Otherwise, pass off to urllib and assume utf-8
            with closing(urlopen(uri)) as url:
                response = url.read().decode("utf-8")
                yaml = YAML()
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
            "source_encoding": directives.encoding,
            "service_type": directives.unchanged,
        },
    )
    parser: Parser

    def run(self):
        relpath, abspath = self.env.relfn2path(
            directives.path(self.arguments[0])
        )

        # env = self.state.document.settings.env
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

        spec: dict[str, Any] = _get_spec(abspath, encoding)
        # spec filename as copied to
        fname: str | None = None

        normalize_spec(spec)

        # if "service_type" in self.options:
        #     st = self.options.get("service_type")
        #     # copy spec under the _static
        #     fname = f"{st}_v{spec['info']['version']}.yaml"
        #     dest = os.path.join(env.app.builder.outdir, "_static", fname)
        #     destdir = os.path.dirname(dest)
        #     if not os.path.exists(destdir):
        #         os.makedirs(destdir)
        #     LOG.info("Copying spec: %s", dest)
        #     copyfile(abspath, dest)

        # Markdown -> docutils parser
        self.parser = Parser()

        results = []

        for hdr in self._get_spec_header_nodes(spec, fname):
            results.append(hdr)

        for tag in spec.get("tags", ["default"]):
            results.append(self._get_api_group_nodes(spec, tag))

        return results

    def _append_markdown_content(self, node, content: str):
        """Parse Markdown `content` and append it to docutils `node`"""
        document = make_document(parser_cls=self.parser)
        self.parser.parse(content, document)
        for child in document:
            node += child

    def _get_spec_header_nodes(
        self, spec: dict[str, Any], fname: str | None = None
    ):
        """Get spec header nodes"""
        yield nodes.version("", spec["info"]["version"])
        # if fname:
        #     yield nodes.field(
        #         "",
        #         nodes.field_name("", "Link"),
        #         nodes.field_body(
        #             "",
        #             nodes.paragraph(
        #                 "",
        #                 "",
        #                 nodes.reference(
        #                     "", nodes.Text(fname), refuri=f"_static/{fname}"
        #                 ),
        #             ),
        #         ),
        #     )
        description = spec["info"].get(
            "description", spec["info"].get("summary")
        )
        if description:
            node = nodes.rubric("")
            self._append_markdown_content(node, description)
            yield node

    def _get_api_group_nodes(self, spec, tag):
        """Process OpenAPI tags (group)"""
        tag_name = tag["name"]
        targetid = f"group-{tag_name}"
        section = nodes.section(
            classes=["api-group", "accordion"], ids=[targetid]
        )
        section += nodes.title(text=tag_name)
        group_descr = tag.get("description", "")
        if group_descr:
            self._append_markdown_content(section, group_descr)

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
        """Process OpenAPI operation"""
        # We might want to have multiple separate entries for single url
        # (a.k.a. actions)
        operation_specs = []
        if not path.endswith("/action"):
            body = (
                operation_spec.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            operation_specs.append((operation_spec, None, body))
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
                            # No action name on the body. Take 1st property
                            # name
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
                # This does not look like an action (no body), just return
                # operation
                operation_specs.append((operation_spec, None, None))

        for operation_spec, action_name, request_body in operation_specs:
            # Iterate over spec and eventual actions
            op_id = (
                operation_spec["operationId"]
                .replace(":", "_")
                .replace("/", "_")
            )
            if action_name:
                op_id += f"-{action_name}"
            op_suffix = ""
            if operation_spec.get("deprecated", False):
                op_suffix = "-deprecated"

            container = nodes.section(
                ids=[f"operation-{op_id}"],
                classes=[
                    "accordion-item",
                    "operation" + op_suffix,
                    "operation-" + method,
                    "gy-2",
                ],
            )
            op_header = openapi_operation_header()
            if not action_name:
                op_header["summary"] = operation_spec.get("summary")
            else:
                if request_body and "summary" in request_body:
                    # For actions we store summary under the body
                    op_header["summary"] = request_body["summary"]
                elif path.endswith("/action") and action_name:
                    op_header["summary"] = f"`{action_name}` action"
                else:
                    op_header["summary"] = request_body.get(
                        "description", f"{action_name} action"
                    )
            op_header["operationId"] = op_id
            op_header["method"] = method
            op_header["path"] = path
            container += op_header
            content = nodes.compound(
                classes=["accordion-collapse collapse accordion-body"],
                ids=[f"collapse{op_id}"],
            )
            descr = operation_spec.get("description")
            if descr:
                self._append_markdown_content(content, descr)
            else:
                # For actions we place their description as body description
                if request_body and "description" in request_body:
                    self._append_markdown_content(
                        content, request_body["description"]
                    )

            content += self._get_operation_request_node(
                op_id, operation_spec, action_name, request_body
            )
            content += self._get_operation_response_node(
                op_id, operation_spec, action_name
            )

            container += content
            yield container

    def _get_operation_request_node(
        self, operationId, operation_spec, action_name=None, request_body=None
    ):
        """Build the Request section"""
        request = nodes.section(ids=[f"api-req-{operationId}"])
        if not request_body:
            return request
        request += nodes.title(text="Request")

        ul = nodes.bullet_list(
            "",
            classes=["nav", "nav-tabs", "requestbody"],
            ids=[f"request_{operationId}"],
        )
        li_table = nodes.list_item(
            "", classes=["nav-item", "requestbody-item"]
        )
        li_table += nodes.rubric(text="Description")
        li_schema = nodes.list_item(
            "", classes=["nav-item", "requestbody-item"]
        )
        li_schema += nodes.rubric(text="Schema")
        ul.extend([li_table, li_schema])
        request += ul

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
        if not request_body:
            body = (
                operation_spec.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
        else:
            body = request_body

        for el in self._get_request_table_field_row(body, None, set()):
            rows.append(el)

        tbody = nodes.tbody()
        tbody.extend(rows)
        tgroup += tbody

        if body:
            for key, sample in body.get("examples", {}).items():
                for el in self._get_body_examples(key, sample):
                    request += el

        if rows:
            li_table += table

        # jsonschema
        jsonschema_pre = nodes.literal_block(
            "", classes=["json", "highlight-javascript"]
        )
        jsonschema_pre.append(
            nodes.literal(
                text=json.dumps(request_body, indent=2),
                language="json",
                classes=["highlight", "code"],
            )
        )
        li_schema += jsonschema_pre

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
                self._append_markdown_content(response, descr)
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
                # Iterate over all available responses to find suitable action
                # response
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
        """Build a row of a request parameters table with the
        parameter/header"""
        tr = nodes.row()
        tr += nodes.entry("", nodes.paragraph(text=param["name"]))
        tr += nodes.entry("", nodes.paragraph(text=param["in"]))
        tr += nodes.entry("", nodes.paragraph(text=param["schema"]["type"]))
        descr = nodes.entry("")
        self._append_markdown_content(descr, param.get("description", ""))
        tr += descr

        return tr

    def _get_request_table_field_row(self, field, field_name, emitted_fields):
        """Emit Request description table row for the body element"""
        if not field:
            return

        typ = field.get("type")
        note = None
        os_ext = field.get("x-openstack", {})
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
            if field_name and field_name not in emitted_fields:
                emitted_fields.add(field_name)
                tr = nodes.row()
                tr += nodes.entry("", nodes.paragraph(text=field_name))
                tr += nodes.entry("", nodes.paragraph(text="body"))
                tr += nodes.entry(
                    "", nodes.paragraph(text=field.get("type", ""))
                )
                td = nodes.entry("")
                self._append_markdown_content(td, param_descr)
                tr += td
                yield tr

            for k, v in field["properties"].items():
                for el in self._get_request_table_field_row(
                    v, f"{field_name}.{k}" if field_name else k, emitted_fields
                ):
                    yield el
        elif typ == "array":
            pass
        elif typ:
            if field_name and field_name not in emitted_fields:
                emitted_fields.add(field_name)
                tr = nodes.row()
                tr += nodes.entry("", nodes.paragraph(text=field_name))
                tr += nodes.entry("", nodes.paragraph(text="body"))
                tr += nodes.entry(
                    "", nodes.paragraph(text=field.get("type", ""))
                )
                td = nodes.entry("")
                self._append_markdown_content(td, param_descr)
                tr += td
                yield tr
        if not typ and "oneOf" in field:
            opts = field["oneOf"]
            discriminator = field.get("x-openstack", {}).get("discriminator")
            if discriminator == "microversion":
                for opt in opts:
                    for el in self._get_request_table_field_row(
                        opt, field_name, emitted_fields
                    ):
                        yield el
            elif discriminator == "action":
                for opt in opts:
                    for el in self._get_request_table_field_row(
                        opt, field_name, emitted_fields
                    ):
                        yield el

    def _get_body_examples(self, sample_key, sample):
        """Add body examples"""
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
    if path and path[0] != "/":
        path = "/" + path
    else:
        path = "/"
    self.body.append(
        '<button class="accordion-button collapsed" type="button" '
        f'data-bs-toggle="collapse" data-bs-target="#collapse{tag_id}" '
        f'aria-expanded="false" aria-controls="collapse{tag_id}">'
    )
    self.body.append('<div class="container">')
    self.body.append('<div class="row">')
    self.body.append(
        f'<div class="col-1"><span class="badge label-{method}">'
        f"{method.upper()}</span></div>"
    )
    self.body.append(
        f'<div class="col-11"><div class="operation-path">{path}</div>'
        f'<div class="operation-summary">{summary or ""}</div></div>'
    )
    self.body.append("</div>")
    self.body.append("</div>")
    self.body.append("</button>")

    raise nodes.SkipNode


def setup(app) -> dict[str, bool]:
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
