import os

# Unfortunately, Sphinx doesn't support code highlighting for standard
# reStructuredText `code` directive. So let's register 'code' directive
# as alias for Sphinx's own implementation.
#
# https://github.com/sphinx-doc/sphinx/issues/2155
from docutils.parsers.rst import directives
from sphinx.directives.code import CodeBlock

directives.register_directive("code", CodeBlock)

project = "openstack-openapi"

extensions = [
    "openstackdocstheme",
    "os_openapi"
]
source_suffix = ".rst"
master_doc = "index"
exclude_patterns = ["_build"]
pygments_style = "default"
html_theme = "openstackdocs"
