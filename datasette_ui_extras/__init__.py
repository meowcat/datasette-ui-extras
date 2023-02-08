import json
import os
import hashlib
import datasette
from datasette import Response
import asyncio
import markupsafe
from urllib.parse import parse_qs
from .plugin import pm
from .hookspecs import hookimpl
from .facets import enable_yolo_facets, facets_extra_body_script
from .filters import enable_yolo_arraycontains_filter, enable_yolo_exact_filter, yolo_filters_from_request
from .new_facets import StatsFacet, YearFacet, YearMonthFacet
from .view_row_pages import enable_yolo_view_row_pages
from .edit_row_pages import enable_yolo_edit_row_pages
from .utils import row_edit_params
from .column_stats import compute_dux_column_stats, autosuggest_column, DUX_COLUMN_STATS, DUX_COLUMN_STATS_VALUES

PLUGIN = 'datasette-ui-extras'

css_files = [
    "app.css",
    "hide-export.css",
    "hide-table-definition.css",
    "sticky-table-headers.css",
    "lazy-facets.css",
    "hide-filters.css",
    "layout-row-page.css",
    "compact-cogs.css",
    "mobile-column-menu.css",
    "edit-row.css",
]

js_files = [
    'hide-filters.js',
    "sticky-table-headers.js",
    "focus-search-box.js",
    'lazy-facets.js',
    'layout-row-page.js',
    "mobile-column-menu.js",
    "edit-row.js",
]

def fingerprint(files, ext):
    def concatenate():
        rv = []
        for fname in files:
            rv.append('/* {} */'.format(fname))
            fpath = os.path.abspath(os.path.join(__file__, '..', 'static', fname))

            f = open(fpath, 'r')
            contents = f.read()
            f.close()
            rv.append(contents)

        return '\n\n'.join(rv)

    hashcode = hashlib.sha256(concatenate().encode('utf-8')).hexdigest()[0:8]

    # TODO: how can we distinguish prod vs dev so we can serve a fingerprinted,
    #       long-lived cached file in prod, but have live reload in dev?
    #path = '/-/{}/{}.{}'.format(PLUGIN, hashcode, ext)
    path = '/-/{}/{}.{}'.format(PLUGIN, ext, ext)

    return path, concatenate

css_path, css_contents = fingerprint(css_files, 'css')
js_path, js_contents = fingerprint(js_files, 'js')

# Not fully fleshed out: datasette-ui-extras consists of a bunch of "extras".
# Each extra has one or more of: custom CSS, custom JS, custom Python.
#
# Try to develop them as though they're standalone things, so they can be
# easily turned on/off, or upstreamed into Datasette.
@datasette.hookimpl
def extra_css_urls(datasette):
    return [
        css_path,
        # https://cdnjs.com/libraries/awesomplete
        'https://cdnjs.cloudflare.com/ajax/libs/awesomplete/1.1.5/awesomplete.min.css',
    ]

@datasette.hookimpl(tryfirst=True)
def extra_js_urls(datasette):
    return [
        js_path,
        # https://cdnjs.com/libraries/awesomplete
        'https://cdnjs.cloudflare.com/ajax/libs/awesomplete/1.1.5/awesomplete.min.js',
    ]

@datasette.hookimpl
def render_cell(datasette, database, table, column, value):
    async def inner():
        task = asyncio.current_task()
        request = None if not hasattr(task, '_duxui_request') else task._duxui_request

        params = await row_edit_params(datasette, request, database, table)
        if params and column in params:
            db = datasette.get_database(database)

            data = params[column]

            default_value = data['default_value']
            default_value_value = None

            if default_value:
                default_value_value = list(await db.execute("SELECT {}".format(default_value)))[0][0]
            control = pm.hook.edit_control(datasette=datasette, database=database, table=table, column=column, metadata=data)

            if control:
                autosuggest_column_url = None
                if 'base_table' in data:
                    base_table = data['base_table']
                    autosuggest_column_url = '{}/-/dux-autosuggest-column'.format(datasette.urls.table(database, base_table))
                return markupsafe.Markup(
                    '<div class="dux-edit-stub" data-database="{database}" data-table="{table}" data-column="{column}" data-control="{control}" data-initial-value="{value}" data-nullable="{nullable}" data-type="{type}" data-default-value="{default_value}" data-default-value-value="{default_value_value}" data-autosuggest-column-url="{autosuggest_column_url}">Loading...</div>'.format(
                        control=markupsafe.escape(control),
                        database=markupsafe.escape(database),
                        table=markupsafe.escape(table),
                        column=markupsafe.escape(column),
                        value=markupsafe.escape(json.dumps(value)),
                        type=markupsafe.escape(data['type']),
                        nullable=markupsafe.escape(json.dumps(data['nullable'])),
                        default_value=markupsafe.escape(json.dumps(default_value)),
                        default_value_value=markupsafe.escape(json.dumps(default_value_value)),
                        autosuggest_column_url=markupsafe.escape(autosuggest_column_url)
                    )
                )

        if isinstance(value, str) and (value == '[]' or (value.startswith('["') and value.endswith('"]'))):
            try:
                tags = json.loads(value)
                rv = ''

                for i, tag in enumerate(tags):
                    if i > 0:
                        rv += ', '
                    rv += markupsafe.Markup(
                        '<span>{tag}</span>'.format(
                            tag=markupsafe.escape(tag)
                        )
                    )

                return rv
            except:
                pass
        return None
    return inner

@datasette.hookimpl
def extra_body_script(template, database, table, columns, view_name, request, datasette):
    return facets_extra_body_script(template, database, table, columns, view_name, request, datasette)

@datasette.hookimpl
def startup(datasette):
    enable_yolo_facets()
    enable_yolo_arraycontains_filter()
    enable_yolo_exact_filter()
    enable_yolo_view_row_pages()
    enable_yolo_edit_row_pages()

    async def inner():
        await compute_dux_column_stats(datasette)

    return inner

@datasette.hookimpl
def register_facet_classes():
    return [StatsFacet, YearFacet, YearMonthFacet]

@datasette.hookimpl
def filters_from_request(request, database, table, datasette):
    async def dothething():
        return await yolo_filters_from_request(request, database, table, datasette)

    return dothething


@datasette.hookimpl(specname='actor_from_request', hookwrapper=True)
def sniff_actor_from_request(datasette, request):
    # TODO: This is temporary, we'll remove it when render_cell gets the request
    # param. The code is committed, just needs a new release of Datasette.
    asyncio.current_task()._duxui_request = request

    # all corresponding hookimpls are invoked here
    outcome = yield

@datasette.hookimpl
def register_routes():
    return [
        (
            css_path,
            lambda: datasette.Response(
                css_contents(),
                content_type="text/css; charset=utf-8"
            )
        ),
        (
            js_path,
            lambda: datasette.Response(
                js_contents(),
                content_type="text/javascript; charset=utf-8"
            )
        ),
        (r"^/(?P<dbname>.*)/(?P<tablename>.*)/-/dux-autosuggest-column$", handle_autosuggest_column)
    ]

async def handle_autosuggest_column(datasette, request):
    qs = parse_qs(request.query_string)

    column = qs['column'][0]
    q = qs.get('q', [''])[0]
    dbname = request.url_vars["dbname"]
    tablename = request.url_vars["tablename"]

    db = datasette.get_database(dbname)

    def fn(conn):
        return autosuggest_column(conn, tablename, column, q)
    suggestions = await db.execute_fn(fn)
    return Response.json(
        suggestions
    )

@datasette.hookimpl
def get_metadata(datasette, key, database, table):
    hide_tables = {
        'tables': {
            DUX_COLUMN_STATS: { 'hidden': True },
            DUX_COLUMN_STATS_VALUES: { 'hidden': True },
        }
    }

    rv = {
        'databases': {
        }
    }

    for db in datasette.databases.keys():
        rv['databases'][db] = hide_tables

    return rv
