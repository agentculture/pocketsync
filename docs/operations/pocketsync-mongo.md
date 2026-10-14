# pocketsync-mongo operations

`pocketsync-mongo` is the MongoDB store for pocketsync: one container running
`mongodb/mongodb-atlas-local` (mongod as a single-node replica set, plus
mongot for Atlas Search and `$vectorSearch`). It is defined in
`deploy/mongo/docker-compose.yml`.

It is independent of the other Mongo containers on the host (`qq-mongodb`
:27017, `eidetic-mongo` :27018, `culture-rules-mongod`, `weather-mongodb`).
Do not reuse or touch them.

## Properties

- Image: `mongodb/mongodb-atlas-local:8.0.34` (pinned; mongod 8.0.34).
- Container name `pocketsync-mongo`, `restart: unless-stopped`.
- Published on `127.0.0.1:27019` only (container port 27017). It is not
  reachable from other machines; use an SSH tunnel if you need that.
- Authentication is on. The root user comes from `deploy/mongo/.env`.
- Data lives on host bind mounts, so removing the container loses nothing.

## First-time setup

```bash
cd deploy/mongo
cp .env.example .env
$EDITOR .env        # set MONGODB_INITDB_ROOT_PASSWORD
```

`.env` is git-ignored. Never commit it. The credentials are only applied when
the data directory is empty (first start); editing them later does not change
an existing database.

Create the host data directories before the first start so they are owned by
you (the image runs as uid 1000, `mongod`; if your uid differs, make the
directories writable by uid 1000):

```bash
D="${POCKETSYNC_DATA_DIR:-${HOME}/.local/share/pocketsync/mongo}"
mkdir -p "$D"/db "$D"/configdb "$D"/mongot
```

## Up and down

```bash
docker compose -f deploy/mongo/docker-compose.yml up -d     # start
docker compose -f deploy/mongo/docker-compose.yml ps        # wait for "healthy"
docker compose -f deploy/mongo/docker-compose.yml logs -f   # logs
docker compose -f deploy/mongo/docker-compose.yml down      # stop and remove the container
```

`down` removes the container, never the data. Do not pass `-v`/`--volumes`;
the mounts are host bind mounts, but keep the habit anyway.

The image has its own healthcheck (`runner healthcheck`), so `ps` shows
`healthy` once mongod and mongot are both up (about a minute on first start).

## Where data lives

Root: `${POCKETSYNC_DATA_DIR:-${HOME}/.local/share/pocketsync/mongo}`. Set
`POCKETSYNC_DATA_DIR` to an absolute path in `.env` or the shell to move it
(compose does not expand `~` inside `.env` values).

| Host path | Container path | Contents |
|-----------|----------------|----------|
| `<root>/db` | `/data/db` | mongod data files, including the replica set config |
| `<root>/configdb` | `/data/configdb` | mongod config database |
| `<root>/mongot` | `/data/mongot` | mongot: search/vector **index definitions** (`configJournal.json`, `indexMetadata/`) and Lucene index files |

Back up by stopping the container (`down`) and copying the whole root.

### Finding: where mongot persists (verified)

The image declares only `/data/db` and `/data/configdb` as `VOLUME`s, but
mongot keeps its state in `/data/mongot` (an empty directory in the image).
Verified on a throwaway container (image 8.0.34, mongot 1.77.0) with all three
directories bind-mounted:

1. Created a collection and a `vectorSearch` index. `/data/mongot` then held
   `configJournal.json`, `indexMetadata/` and a Lucene directory per index.
2. `docker rm -f`, then re-created on the same directories: the document
   count, `getSearchIndexes()` (queryable) and a `$vectorSearch` query all
   returned the same results.
3. Control: re-created with `/data/mongot` pointed at a fresh empty directory
   and the other two unchanged: `getSearchIndexes()` returned `[]`. The search
   index definitions were lost. So the `/data/mongot` mount is required, not
   optional.

### Finding: the container hostname must be fixed

The single-node replica set stores its member host name in `/data/db`. With a
random container hostname, a re-created container started as `RSGhost`
("ReplicaSetNoPrimary"), never became healthy and nothing could be read. The
compose file therefore sets `hostname: pocketsync-mongo`. Do not remove it.

## Authentication check

An unauthenticated connection must be refused for reads and writes. From the
host (needs `mongosh`), or substitute `docker exec pocketsync-mongo mongosh`:

```bash
mongosh --quiet "mongodb://127.0.0.1:27019/test" --eval 'db.c.countDocuments()'
# MongoServerError: Command aggregate requires authentication
```

Authenticated:

```bash
mongosh --quiet "mongodb://pocketsync:<password>@127.0.0.1:27019/test?authSource=admin" \
  --eval 'db.runCommand({ping: 1})'
```

Also confirm the port is loopback-only:

```bash
ss -ltn | grep 27019        # expect 127.0.0.1:27019, never 0.0.0.0 or [::]
```

## Re-create check (data survives the container)

Run after the first real data lands, and after any change to the compose
file. Replace `<password>` and keep the database/collection names as needed:

```bash
URI="mongodb://pocketsync:<password>@127.0.0.1:27019/pocketsync?authSource=admin"
Q='print("count", db.recordings.countDocuments());
   printjson(db.recordings.find({}, {_id: 1}).sort({_id: 1}).limit(3).toArray());
   printjson(db.recordings.getSearchIndexes().map(i => ({name: i.name, queryable: i.queryable})))'

mongosh --quiet "$URI" --eval "$Q" | tee /tmp/before.txt

docker compose -f deploy/mongo/docker-compose.yml down
docker compose -f deploy/mongo/docker-compose.yml up -d
# wait until: docker compose -f deploy/mongo/docker-compose.yml ps  shows healthy

mongosh --quiet "$URI" --eval "$Q" | tee /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt && echo "re-create check passed"
```

The count, the first documents and the search index list (still `queryable`)
must be identical. A search index that is missing or not yet queryable
immediately after start can take a few seconds to settle; re-run once before
treating it as a failure.
