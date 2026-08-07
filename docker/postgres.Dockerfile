# PostgreSQL with both extensions FastFort knows how to use.
#
# No published image carries PostGIS and pgvector together, and a project using
# geography columns and embeddings in the same database needs both -- so the
# test and sandbox stacks build one. Two apt packages on top of the PostGIS
# image, which is itself PostgreSQL plus one.
#
# The tests do not require this. Anything spatial and anything vector asks the
# server whether its extension is installed and skips when it is not, so a plain
# `postgres:16` still runs the rest of the suite green -- which is exactly what
# CI does. This image is what makes those skipped tests actually run.
FROM postgis/postgis:16-3.4

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*
