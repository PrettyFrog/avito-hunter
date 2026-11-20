# Historical migration snapshots

These compressed SQL files are frozen schema DDL copied from repository history,
not synthesized from the current schema. The version is the schema version at
the referenced revision:

| Snapshot | Source revision |
|---|---|
| v1 | `cfdfbf0^:src/hunter/core/schema.sql` |
| v6 | `62bebde:src/hunter/core/schema.sql` |
| v11 | `adfd12a:src/hunter/core/schema.sql` |
| v12 | `ab02258^:src/hunter/core/schema.sql` |
| v14 | `8853782:src/hunter/core/schema.sql` |
| v16 | `70c14cd:src/hunter/core/schema.sql` |

The integration matrix creates each database from its frozen DDL, sets the
matching SQLite `user_version`, seeds one historical listing, then exercises
the production `Database.connect()` migration path. Provenance revisions are
present in this repository's Git history.
