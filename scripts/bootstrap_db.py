#!/usr/bin/env python3
import logging
import os
import pathlib
import sys

import psycopg2

from funcomdb.config import Connection, Credentials
from ToolsDB.settings import Settings
from ToolsDB.setupdb import setupdb
from secret_env import read_secret_env


HOST = "postgres"
PORT = 5432
ADMIN_DATABASE = "postgres"
DATABASE = "dune_sb_1_4_0_0"
USER = "dune"
PASSWORD = read_secret_env("POSTGRES_DUNE_PASSWORD")
if not PASSWORD:
    raise RuntimeError("POSTGRES_DUNE_PASSWORD is required")
SCHEMA = "dune"
SCHEMA_PATH = pathlib.Path("/root/DuneSandbox/Database")


def database_exists() -> bool:
    conn = psycopg2.connect(host=HOST, port=PORT, database=ADMIN_DATABASE, user=USER, password=PASSWORD)
    try:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("select 1 from pg_database where datname = %s", (DATABASE,))
            return cursor.fetchone() is not None
    finally:
        conn.close()


def create_database() -> None:
    conn = psycopg2.connect(host=HOST, port=PORT, database=ADMIN_DATABASE, user=USER, password=PASSWORD)
    try:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(f'create database "{DATABASE}" owner "{USER}"')
    finally:
        conn.close()


def schema_initialized() -> bool:
    with psycopg2.connect(host=HOST, port=PORT, database=DATABASE, user=USER, password=PASSWORD) as conn:
        with conn.cursor() as cursor:
            cursor.execute("select 1 from information_schema.schemata where schema_name = %s", (SCHEMA,))
            return cursor.fetchone() is not None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    log = logging.getLogger("dune-db-bootstrap")

    if not database_exists():
        log.info("Creating database %s", DATABASE)
        create_database()

    if schema_initialized():
        log.info("Schema %s already exists in %s", SCHEMA, DATABASE)
        with psycopg2.connect(host=HOST, port=PORT, database=DATABASE, user=USER, password=PASSWORD) as conn:
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute(f'alter database "{DATABASE}" set search_path to {SCHEMA}, public')
        return 0

    settings = Settings(
        bin_path=pathlib.Path("/usr/bin"),
        connection=Connection(host=HOST, port=PORT, timeout=30),
        user_credentials=Credentials(user=USER, password=PASSWORD, database=DATABASE),
        admin_credentials=Credentials(user=USER, password=PASSWORD, database=ADMIN_DATABASE),
        schema_name=SCHEMA,
        module_path=SCHEMA_PATH,
        tables_to_dump=["applied_patches"],
        extra_schema_names=["ext"],
    )

    if not setupdb(log, settings):
        return 1

    with psycopg2.connect(host=HOST, port=PORT, database=DATABASE, user=USER, password=PASSWORD) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(f'alter database "{DATABASE}" set search_path to {SCHEMA}, public')

    log.info("Database bootstrap complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
