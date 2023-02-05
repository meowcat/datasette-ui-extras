from datasette.database import Database
from sqlite_utils.db import View, Table, validate_column_names, jsonify_if_needed
from sqlglot import parse_one, exp

# Adds two features:
# - can navigate to the row page for a view row, eg /db/viewname/1
# - can use the JSON API to do an update of a view row (...assumes there's an INSTEAD OF trigger)

original_primary_keys = Database.primary_keys

async def patched_primary_keys(self, table):
    # Is it a view?
    # Get its definition
    # Parse it with sqlglot
    # Is it a select that queries exactly one table?
    is_view = list(await self.execute("select sql from sqlite_master where type = 'view' and name = ?", [table]))

    if not is_view:
        return await original_primary_keys(self, table)

    sql = is_view[0]['sql']

    parsed = None
    try:
        parsed = parse_one(sql)
    except:
        return []

    #print(repr(parsed))

    view_exp = parsed.expression

    if not 'from' in view_exp.args:
        return []

    from_exps = view_exp.args['from'].expressions

    if len(from_exps) != 1:
        return []

    table_name = from_exps[0].this.this

    table_pkeys = await original_primary_keys(self, table_name)

    cols = view_exp.expressions

    # We only want the columns that are pass-through identifiers
    ids = []
    for col in cols:
        this = col.this
        if isinstance(col, exp.Column) and isinstance(col.this, exp.Identifier):
            ids.append(col.name)


    #print('ids={} table_pkeys={}'.format(ids, table_pkeys))

    # If every table pkey is in ids, return table_pkeys.
    ok = True
    for pkey in table_pkeys:
        ok = ok and pkey in ids

    if ok:
        return table_pkeys

    return []

original_get_all_foreign_keys = Database.get_all_foreign_keys
async def patched_get_all_foreign_keys(self):
    rv = await original_get_all_foreign_keys(self)

    names = await self.execute("select name from sqlite_master where type = 'view'")
    for name, in names:
        rv[name] = {'incoming': [], 'outgoing': []}

    return rv

class UpdateableView(View):
    def __init__(self, underlying):
        self.underlying = underlying

    @property
    def pks(self):
        "Primary key columns for this view."
        # TODO: figure this out intelligently
        return ['id']

        names = [column.name for column in self.columns if column.is_pk]
        if not names:
            names = ["rowid"]
        #print('pks={}'.format(names))
        return names

    def __getattr__(self, name):
        return getattr(self.underlying, name)

    def update(
        self,
        pk_values,
        updates = None,
        alter = False,
        conversions = None,
    ):
        """
        Execute a SQL ``UPDATE`` against the specified row.

        See :ref:`python_api_update`.

        :param pk_values: The primary key of an individual record - can be a tuple if the
          table has a compound primary key.
        :param updates: A dictionary mapping columns to their updated values.
        :param alter: Set to ``True`` to add any missing columns.
        :param conversions: Optional dictionary of SQL functions to apply during the update, for example
          ``{"mycolumn": "upper(?)"}``.
        """
        updates = updates or {}
        conversions = conversions or {}
        if not isinstance(pk_values, (list, tuple)):
            pk_values = [pk_values]
        # Soundness check that the record exists (raises error if not):
        self.get(pk_values)
        if not updates:
            return self
        args = []
        sets = []
        wheres = []
        pks = self.pks
        validate_column_names(updates.keys())
        for key, value in updates.items():
            sets.append("[{}] = {}".format(key, conversions.get(key, "?")))
            args.append(jsonify_if_needed(value))
        wheres = ["[{}] = ?".format(pk_name) for pk_name in pks]
        args.extend(pk_values)
        sql = "update [{table}] set {sets} where {wheres}".format(
            table=self.name, sets=", ".join(sets), wheres=" and ".join(wheres)
        )
        #print('running update: sql={} args={}'.format(sql, args))
        with self.db.conn:
            try:
                rowcount = self.db.execute(sql, args).rowcount
            except OperationalError as e:
                if alter and (" column" in e.args[0]):
                    # Attempt to add any missing columns, then try again
                    self.add_missing_columns([updates])
                    rowcount = self.db.execute(sql, args).rowcount
                else:
                    raise

            # NOTE: Don't check rowcount, updates on updatable views don't have a rowcount.
            # assert rowcount == 1
        self.last_pk = pk_values[0] if len(pks) == 1 else pk_values
        return self


def thunk_update(
    self,
    pk_values,
    updates = None,
    alter = False,
    conversions = None,
):

    if alter:
        raise Exception('updates on view must have alter=False')

    if conversions:
        raise Exception('updates on view must have conversions=None')

    uv = UpdateableView(self)
    return uv.update(pk_values, updates, alter=False, conversions=None)

def enable_yolo_view_row_pages():
    Database.primary_keys = patched_primary_keys
    Database.get_all_foreign_keys = patched_get_all_foreign_keys

    # Enable updating views in the JSON API. I didn't want to snapshot the code,
    # nor generally .get and .pks on views, so I do some shenanigans here to
    # add an .update method that gets hijacked by UpdateableView.
    #
    # I ended up having to snapshot the code, because the existing stuff did
    # an assert on the rowcount -- which is not correct for INSTEAD OF triggers.
    UpdateableView.get = Table.get
    View.update = thunk_update
