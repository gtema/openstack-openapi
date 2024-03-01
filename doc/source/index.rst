==============
OpenStack APIs
==============

This project (currently in a POC phase) serves 2 different purposes:

- stores individual OpenAPI specs for the services (it feels more logical
  to have a central storage of specs to make it easier for the consumers to
  find them and to enforce certain spec rules, especially with OpenStack
  APIs being not very OpenAPI conform)

- implement a Sphinx extension to render the specs into the HTML. Currently
  it does so in a style that is a mix of a Swagger style and old os-api-ref
  style for OpenStack.

OpenAPI specifics
=================

Not all OpenStack APIs are fitting properly into the OpenAPI specification. In order still be able to provide the OpenAPI specs for those services certain decisions (workarounds) have been made

Microversion
~~~~~~~~~~~~

A concept of microversions in OpenStack is allowing using of different
operation schema depending on the version header. This is not very well
addressed by OpenAPI, but also a workaround for that is existing. Since
OpenAPI bases on the JsonSchema 3.1 it is possible to use "oneOf" construct
to describe different schemas. In order for the OpenStack tooling to be able to describe and recognize this properly a it is required to mark such schema with custom "x-" extension

.. code-block:: yaml

   components:
     schemas:
       foo_with_mv:
         oneOf:
           - $ref: #/components/schemas/foo_v1
           - $ref: #/components/schemas/foo_v21
           - $ref: #/components/schemas/foo_v220
         x-openstack:
           discriminator: microversion
       foo_v21:
         type: object
         properties:
           - foo:
               type: string
         x-openstack:
           min-ver: 2.1
           max-ver: 2.19
       foo_v220:
         type: object
         properties:
           - foo:
               type: string
         x-openstack:
           min-ver: 2.20

.. note::

   `min-ver` and `max-ver` properties are having the same
   meaning as in the services: starting with which microversion
   the schema has been added and till which microversion it
   eventually is valid


Action
~~~~~~

Minority of OpenStack services (but in the most widely used places) have a
concept of actions. This was inspired by RPC where depending on the operation payload different actions are being performed.

OpenAPI is currently strictly requiring that a combination of URL + http
method must be unique. Since Actions require quite opposite also here a
similar solution like for microversions can be applied.

.. code-block:: yaml

   components:
     schemas:
       server_actions:
         oneOf:
           - $ref: #/components/schemas/action_foo
           - $ref: #/components/schemas/action_bar
         x-openstack:
           discriminator: action
       action_foo:
         type: object
         properties:
           - foo:
               type: string
         x-openstack:
           action-name: foo
           min-ver: 2.1
           max-ver: 2.19
       action_bar:
         type: object
         properties:
           - bar:
               type: integer
         x-openstack:
           action-name: bar
           min-ver: 2.20

.. note:: it is possible even to combine those methods when a certain action
   is also supporting different microversions. For this on a first level
   there is still an "action" discriminator is being used and the action body
   schema itself is also an "oneOf" schema setting discriminator to
   "microversion".

Flexible HTTP headers
~~~~~~~~~~~~~~~~~~~~~

Mostly Swift is allowing custom headers both in request and response. In the
current form OpenAPI requires that all headers are explicitly described. In
order to deal with this situation a "regexp" form of the headers can be user.

.. code-block:: yaml

   ...
      responses:
        '200':
          description: OK
          headers:
            X-Account-Meta-*:
              $ref: '#/components/headers/X-Account-Meta'
   components:
     headers:
       X-Account-Meta:
         x-openstack:
           style: regex
         ...


Path requirements
=================

For a long time (and still in some places) Services declare their APIs
requiring some form of `project_id` as part of the operation URL. Ohers place
version prefix while yet others do not. In order to bring consistency and fit
specs into the OpenAPI concept it is required that version prefix IS part of
the url. This brings assumption to the tooling relying on the specs that the
URL is appended behind the "version discovery" endpoint of the service. The
tooling is, however, advised to apply additional logic of avoiding certain
path elements duplication when service catalog points to the versioned
service endpoint. This requirement helps solving routing issues in the client
facing tool with determination of a service "root".

The spec is also defining the API version (which may look like "2.92" to
communicate maximal microversion)

Spec generation
===============

All specs provided here are generated automatically from the source code of the services using `openstack-codegenerator <https://gtema.github.io/openstack-codegenerator>`_ project. It is a conscious decision not to deal with specs manually due to their size, complexity and the issues described above.

Development state
=================

At the moment all specs (except object-store) are created automatically. HTML
rendering is at a very early stage not properly implementing actions and
microversions rendering and instead renders every URL like the Swagger would
do. This is going to change once more time is going to be invested on this
front.

.. toctree::
   :maxdepth: 1

   block-storage
   compute
   identity
   image
   load-balancing
   network
   object-store
   placement
