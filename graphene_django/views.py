import inspect
import json
import re

from django.http import HttpResponse
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views.generic import View
from django.views.decorators.csrf import ensure_csrf_cookie

from graphql.type.schema import GraphQLSchema
from graphql_server import run_http_query, HttpQueryError, default_format_error, load_json_body

from .settings import graphene_settings


class HttpError(Exception):

    def __init__(self, response, message=None, *args, **kwargs):
        self.response = response
        self.message = message = message or response.content.decode()
        super(HttpError, self).__init__(message, *args, **kwargs)


def get_accepted_content_types(request):
    def qualify(x):
        parts = x.split(';', 1)
        if len(parts) == 2:
            match = re.match(r'(^|;)q=(0(\.\d{,3})?|1(\.0{,3})?)(;|$)',
                             parts[1])
            if match:
                return parts[0], float(match.group(2))
        return parts[0], 1

    raw_content_types = request.META.get('HTTP_ACCEPT', '*/*').split(',')
    qualified_content_types = map(qualify, raw_content_types)
    return list(x[0] for x in sorted(qualified_content_types,
                                     key=lambda x: x[1], reverse=True))


def instantiate_middleware(middlewares):
    for middleware in middlewares:
        if inspect.isclass(middleware):
            yield middleware()
            continue
        yield middleware


def flatten(multidict):
    return {x:multidict.get(x) for x in multidict.keys()}


class GraphQLView(View):
    schema = None
    executor = None
    root_value = None
    context = None
    pretty = False
    graphiql = False
    graphiql_version = '0.7.8'
    graphiql_template = 'graphene/graphiql.html'
    middleware = None
    batch = True

    def __init__(self, schema=None, executor=None, middleware=None, root_value=None, graphiql=False, pretty=False,
                 batch=False):
        if not schema:
            schema = graphene_settings.SCHEMA

        if middleware is None:
            middleware = graphene_settings.MIDDLEWARE

        self.schema = schema
        if middleware is not None:
            self.middleware = list(instantiate_middleware(middleware))
        self.executor = executor
        self.root_value = root_value
        self.pretty = pretty
        self.graphiql = graphiql
        self.batch = batch

        assert isinstance(self.schema, GraphQLSchema), 'A Schema is required to be provided to GraphQLView.'

    def get_root_value(self, request):
        return self.root_value

    def get_middleware(self, request):
        return self.middleware

    def get_context(self, request):
        return request

    def get_executor(self):
        return self.executor

    def render_graphiql(self, request, params, result):
        return render(request, self.graphiql_template, dict(
            params=params,
            result=result,
        ))

    @method_decorator(ensure_csrf_cookie)
    def dispatch(self, request, *args, **kwargs):
        try:
            request_method = request.method.lower()
            get_params = flatten(request.GET)
            data = self.parse_body(request)

            if isinstance(data, dict):
                data = dict(data, **get_params)

            show_graphiql = request_method == 'get' and self.should_display_graphiql(request)
            catch = HttpQueryError if show_graphiql else None

            pretty = self.pretty or show_graphiql or request.GET.get('pretty')

            result, status_code, all_params = run_http_query(
                self.schema,
                request_method,
                data,
                batch_enabled=self.batch,
                catch=catch,

                # Execute options
                root_value=self.get_root_value(request),
                context_value=self.get_context(request),
                middleware=self.get_middleware(request),
                executor=self.get_executor(),
            )

            result = self.json_encode(result, pretty)

            if show_graphiql:
                return self.render_graphiql(
                    request,
                    params=all_params[0],
                    result=result
                )

            return HttpResponse(
                result,
                status=status_code,
                content_type='application/json'
            )

        except HttpQueryError as e:
            response = HttpResponse(
                self.json_encode({
                    'errors': [default_format_error(e)]
                }),
                status=e.status_code,
                content_type='application/json'
            )
            if e.headers:
                for name, value in e.headers.items():
                    response[name] = value
            return response

    @staticmethod
    def get_content_type(request):
        meta = request.META
        content_type = meta.get('CONTENT_TYPE', meta.get('HTTP_CONTENT_TYPE', ''))
        return content_type.split(';', 1)[0].lower()

    # noinspection PyBroadException
    def parse_body(self, request):
        # We use mimetype here since we don't need the other
        # information provided by content_type
        content_type = self.get_content_type(request)
        if content_type == 'application/graphql':
            return {'query': request.body.decode()}

        elif content_type == 'application/json':
            return load_json_body(request.body.decode('utf-8'))

        elif content_type == 'application/x-www-form-urlencoded' \
          or content_type == 'multipart/form-data':
            return flatten(request.POST)

        return {}

    @staticmethod
    def json_encode(data, pretty=False):
        if not pretty:
            return json.dumps(data, separators=(',', ':'))

        return json.dumps(
            data,
            indent=2,
            separators=(',', ': ')
        )

    def should_display_graphiql(self, request):
        if not self.graphiql or 'raw' in request.GET:
            return False

        return self.request_wants_html(request)

    @classmethod
    def request_wants_html(cls, request):
        accepted = get_accepted_content_types(request)
        html_index = accepted.count('text/html')
        json_index = accepted.count('application/json')

        return html_index > json_index
