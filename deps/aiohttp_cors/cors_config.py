# Copyright 2015 Vladimir Rutsky <vladimir@rutsky.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""CORS configuration container class definition.
"""

import asyncio
import collections
from typing import Mapping, Union, Any

from aiohttp import hdrs, web

from .urldispatcher_router_adapter import OldRoutesUrlDispatcherRouterAdapter
from .urldispatcher_router_adapter import ResourcesUrlDispatcherRouterAdapter
from .abc import AbstractRouterAdapter
from .resource_options import ResourceOptions

__all__ = (
    "CorsConfig",
)


# Positive response to Access-Control-Allow-Credentials
_TRUE = "true"
# CORS simple response headers:
# <http://www.w3.org/TR/cors/#simple-response-header>
_SIMPLE_RESPONSE_HEADERS = frozenset([
    hdrs.CACHE_CONTROL,
    hdrs.CONTENT_LANGUAGE,
    hdrs.CONTENT_TYPE,
    hdrs.EXPIRES,
    hdrs.LAST_MODIFIED,
    hdrs.PRAGMA
])


def _parse_config_options(
        config: Mapping[str, Union[ResourceOptions, Mapping[str, Any]]]=None):
    """Parse CORS configuration (default or per-route)

    :param config:
        Mapping from Origin to Resource configuration (allowed headers etc)
        defined either as mapping or `ResourceOptions` instance.

    Raises `ValueError` if configuration is not correct.
    """

    if config is None:
        return {}

    if not isinstance(config, collections.abc.Mapping):
        raise ValueError(
            "Config must be mapping, got '{}'".format(config))

    parsed = {}

    options_keys = {
        "allow_credentials", "expose_headers", "allow_headers", "max_age"
    }

    for origin, options in config.items():
        # TODO: check that all origins are properly formatted.
        # This is not a security issue, since origin is compared as strings.
        if not isinstance(origin, str):
            raise ValueError(
                "Origin must be string, got '{}'".format(origin))

        if isinstance(options, ResourceOptions):
            resource_options = options

        else:
            if not isinstance(options, collections.abc.Mapping):
                raise ValueError(
                    "Origin options must be either "
                    "aiohttp_cors.ResourceOptions instance or mapping, "
                    "got '{}'".format(options))

            unexpected_args = frozenset(options.keys()) - options_keys
            if unexpected_args:
                raise ValueError(
                    "Unexpected keywords in resource options: {}".format(
                        # pylint: disable=bad-builtin
                        ",".join(map(str, unexpected_args))))

            resource_options = ResourceOptions(**options)

        parsed[origin] = resource_options

    return parsed


_ConfigType = Mapping[str, Union[ResourceOptions, Mapping[str, Any]]]


class _CorsConfigImpl:

    def __init__(self,
                 app: web.Application,
                 router_adapter: AbstractRouterAdapter):
        self._app = app

        self._router_adapter = router_adapter

        # Register hook for all responses.  This hook handles CORS-related
        # headers on non-preflight requests.
        self._app.on_response_prepare.append(self._on_response_prepare)

    def add(self,
            routing_entity,
            config: _ConfigType=None):
        """Enable CORS for specific route or resource.

        If route is passed CORS is enabled for route's resource.

        :param routing_entity:
            Route or Resource for which CORS should be enabled.
        :param config:
            CORS options for the route.
        :return: `routing_entity`.
        """

        parsed_config = _parse_config_options(config)

        self._router_adapter.add_preflight_handler(
            routing_entity, self._preflight_handler)
        self._router_adapter.set_config_for_routing_entity(
            routing_entity, parsed_config)

        return routing_entity

    @asyncio.coroutine
    def _on_response_prepare(self,
                             request: web.Request,
                             response: web.StreamResponse):
        """Non-preflight CORS request response processor.

        If request is done on CORS-enabled route, process request parameters
        and set appropriate CORS response headers.
        """
        if (not self._router_adapter.is_cors_enabled_on_request(request) or
                self._router_adapter.is_preflight_request(request)):
            # Either not CORS enabled route, or preflight request which is
            # handled in its own handler.
            return

        # Processing response of non-preflight CORS-enabled request.

        config = self._router_adapter.get_non_preflight_request_config(request)

        # Handle according to part 6.1 of the CORS specification.

        origin = request.headers.get(hdrs.ORIGIN)
        if origin is None:
            # Terminate CORS according to CORS 6.1.1.
            return

        options = config.get(origin, config.get("*"))
        if options is None:
            # Terminate CORS according to CORS 6.1.2.
            return

        assert hdrs.ACCESS_CONTROL_ALLOW_ORIGIN not in response.headers
        assert hdrs.ACCESS_CONTROL_ALLOW_CREDENTIALS not in response.headers
        assert hdrs.ACCESS_CONTROL_EXPOSE_HEADERS not in response.headers

        # Process according to CORS 6.1.4.
        # Set exposed headers (server headers exposed to client) before
        # setting any other headers.
        if options.expose_headers == "*":
            # Expose all headers that are set in response.
            exposed_headers = \
                frozenset(response.headers.keys()) - _SIMPLE_RESPONSE_HEADERS
            response.headers[hdrs.ACCESS_CONTROL_EXPOSE_HEADERS] = \
                ",".join(exposed_headers)

        elif options.expose_headers:
            # Expose predefined list of headers.
            response.headers[hdrs.ACCESS_CONTROL_EXPOSE_HEADERS] = \
                ",".join(options.expose_headers)

        # Process according to CORS 6.1.3.
        # Set allowed origin.
        response.headers[hdrs.ACCESS_CONTROL_ALLOW_ORIGIN] = origin
        if options.allow_credentials:
            # Set allowed credentials.
            response.headers[hdrs.ACCESS_CONTROL_ALLOW_CREDENTIALS] = _TRUE

    @staticmethod
    def _parse_request_method(request: web.Request):
        """Parse Access-Control-Request-Method header of the preflight request
        """
        method = request.headers.get(hdrs.ACCESS_CONTROL_REQUEST_METHOD)
        if method is None:
            raise web.HTTPForbidden(
                text="CORS preflight request failed: "
                     "'Access-Control-Request-Method' header is not specified")

        # FIXME: validate method string (ABNF: method = token), if parsing
        # fails, raise HTTPForbidden.

        return method

    @staticmethod
    def _parse_request_headers(request: web.Request):
        """Parse Access-Control-Request-Headers header or the preflight request

        Returns set of headers in upper case.
        """
        headers = request.headers.get(hdrs.ACCESS_CONTROL_REQUEST_HEADERS)
        if headers is None:
            return frozenset()

        # FIXME: validate each header string, if parsing fails, raise
        # HTTPForbidden.
        # FIXME: check, that headers split and stripped correctly (according
        # to ABNF).
        headers = (h.strip(" \t").upper() for h in headers.split(","))
        # pylint: disable=bad-builtin
        return frozenset(filter(None, headers))

    @asyncio.coroutine
    def _preflight_handler(self, request: web.Request):
        """CORS preflight request handler"""

        # Handle according to part 6.2 of the CORS specification.

        origin = request.headers.get(hdrs.ORIGIN)
        if origin is None:
            # Terminate CORS according to CORS 6.2.1.
            raise web.HTTPForbidden(
                text="CORS preflight request failed: "
                     "origin header is not specified in the request")

        # CORS 6.2.3. Doing it out of order is not an error.
        request_method = self._parse_request_method(request)

        # CORS 6.2.5. Doing it out of order is not an error.

        try:
            config = \
                yield from self._router_adapter.get_preflight_request_config(
                    request, origin, request_method)
        except KeyError:
            raise web.HTTPForbidden(
                text="CORS preflight request failed: "
                     "request method {!r} is not allowed "
                     "for {!r} origin".format(request_method, origin))

        if not config:
            # No allowed origins for the route.
            # Terminate CORS according to CORS 6.2.1.
            raise web.HTTPForbidden(
                text="CORS preflight request failed: "
                     "no origins are allowed")

        options = config.get(origin, config.get("*"))
        if options is None:
            # No configuration for the origin - deny.
            # Terminate CORS according to CORS 6.2.2.
            raise web.HTTPForbidden(
                text="CORS preflight request failed: "
                     "origin '{}' is not allowed".format(origin))

        # CORS 6.2.4
        request_headers = self._parse_request_headers(request)

        # CORS 6.2.6
        if options.allow_headers == "*":
            pass
        else:
            disallowed_headers = request_headers - options.allow_headers
            if disallowed_headers:
                raise web.HTTPForbidden(
                    text="CORS preflight request failed: "
                         "headers are not allowed: {}".format(
                             ", ".join(disallowed_headers)))

        # Ok, CORS actual request with specified in the preflight request
        # parameters is allowed.
        # Set appropriate headers and return 200 response.

        response = web.Response()

        # CORS 6.2.7
        response.headers[hdrs.ACCESS_CONTROL_ALLOW_ORIGIN] = origin
        if options.allow_credentials:
            # Set allowed credentials.
            response.headers[hdrs.ACCESS_CONTROL_ALLOW_CREDENTIALS] = _TRUE

        # CORS 6.2.8
        if options.max_age is not None:
            response.headers[hdrs.ACCESS_CONTROL_MAX_AGE] = \
                str(options.max_age)

        # CORS 6.2.9
        # TODO: more optimal for client preflight request cache would be to
        # respond with ALL allowed methods.
        response.headers[hdrs.ACCESS_CONTROL_ALLOW_METHODS] = request_method

        # CORS 6.2.10
        if request_headers:
            # Note: case of the headers in the request is changed, but this
            # shouldn't be a problem, since the headers should be compared in
            # the case-insensitive way.
            response.headers[hdrs.ACCESS_CONTROL_ALLOW_HEADERS] = \
                ",".join(request_headers)

        return response


class CorsConfig:
    """CORS configuration instance.

    The instance holds default CORS parameters and per-route options specified
    in `add()` method.

    Each `aiohttp.web.Application` can have exactly one instance of this class.
    """

    def __init__(self, app: web.Application, *,
                 defaults: _ConfigType=None,
                 router_adapter: AbstractRouterAdapter=None):
        """Construct CORS configuration.

        :param app:
            Application for which CORS configuration is built.
        :param defaults:
            Default CORS settings for origins.
        :param router_adapter:
            Router adapter. Required if application uses non-default router.
        """

        defaults = _parse_config_options(defaults)

        self._cors_impl = None

        self._resources_router_adapter = None
        self._resources_cors_impl = None

        self._old_routes_cors_impl = None

        if router_adapter is not None:
            self._cors_impl = _CorsConfigImpl(app, router_adapter)

        elif isinstance(app.router, web.UrlDispatcher):
            self._resources_router_adapter = \
                ResourcesUrlDispatcherRouterAdapter(app.router, defaults)
            self._resources_cors_impl = _CorsConfigImpl(
                app,
                self._resources_router_adapter)
            self._old_routes_cors_impl = _CorsConfigImpl(
                app,
                OldRoutesUrlDispatcherRouterAdapter(app.router, defaults))
        else:
            raise RuntimeError(
                "Router adapter is not specified. "
                "Routers other than aiohttp.web.UrlDispatcher requires"
                "custom router adapter.")

    def add(self,
            routing_entity,
            config: _ConfigType = None):
        """Enable CORS for specific route or resource.

        If route is passed CORS is enabled for route's resource.

        :param routing_entity:
            Route or Resource for which CORS should be enabled.
        :param config:
            CORS options for the route.
        :return: `routing_entity`.
        """

        if self._cors_impl is not None:
            # Custom router adapter.
            return self._cors_impl.add(routing_entity, config)

        else:
            # UrlDispatcher.

            if isinstance(routing_entity, (web.Resource, web.StaticResource)):
                # New Resource - use new router adapter.
                return self._resources_cors_impl.add(routing_entity, config)

            elif isinstance(routing_entity, web.AbstractRoute):
                if self._resources_router_adapter.is_cors_for_resource(
                        routing_entity.resource):
                    # Route which resource has CORS configuration in
                    # new-style router adapter.
                    return self._resources_cors_impl.add(
                        routing_entity, config)
                else:
                    # Route which resource has no CORS configuration, i.e.
                    # old-style route.
                    return self._old_routes_cors_impl.add(
                        routing_entity, config)

            else:
                raise ValueError(
                    "Unknown resource/route type: {!r}".format(routing_entity))
