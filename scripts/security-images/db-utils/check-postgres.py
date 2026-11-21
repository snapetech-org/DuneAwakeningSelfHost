"""Exercise the vendor PostgreSQL driver against an isolated TLS SQL fixture."""
import io
import json
import ssl
import time

import psycopg2
import psycopg2.extras


def main():
    options = dict(host='localhost', hostaddr='127.0.0.1', dbname='postgres',
                   user='postgres', sslmode='verify-full',
                   sslrootcert='/fixture/server.crt', connect_timeout=3)
    deadline = time.monotonic() + 45
    while True:
        try:
            connection = psycopg2.connect(**options)
            break
        except psycopg2.OperationalError:
            if time.monotonic() >= deadline:
                raise RuntimeError('isolated-sql-tls-fixture-not-ready') from None
            time.sleep(1)
    try:
        with connection.cursor() as cursor:
            cursor.execute('select ssl from pg_stat_ssl where pid=pg_backend_pid()')
            assert cursor.fetchone() == (True,)
            cursor.execute('create temporary table native_fixture(id integer primary key, payload jsonb, payload_bytes bytea)')
            cursor.execute('insert into native_fixture values(%s,%s,%s)',
                           (1, psycopg2.extras.Json({'native': True}), psycopg2.Binary(b'\x00\xfftest')))
        connection.commit()
        try:
            with connection.cursor() as cursor:
                cursor.execute('insert into native_fixture values(%s,%s,%s)', (1, '{}', b''))
        except psycopg2.errors.UniqueViolation:
            connection.rollback()
        else:
            raise AssertionError('uniqueness-error-not-raised')
        with connection.cursor() as cursor:
            cursor.execute('select id,payload,payload_bytes from native_fixture')
            row = cursor.fetchone()
            assert row[:2] == (1, {'native': True}) and bytes(row[2]) == b'\x00\xfftest'
            cursor.execute('create temporary table copy_fixture(id integer, value text)')
            cursor.copy_expert('copy copy_fixture from stdin with csv', io.StringIO('2,fixture\n3,second\n'))
            cursor.execute('select * from copy_fixture order by id')
            assert cursor.fetchall() == [(2, 'fixture'), (3, 'second')]
    finally:
        connection.close()
    try:
        connection = psycopg2.connect(**dict(options, host='incorrect.fixture.invalid'))
    except psycopg2.OperationalError:
        pass
    else:
        connection.close()
        raise AssertionError('incorrect-certificate-hostname-accepted')
    print(json.dumps({'checks': ['verified-tls', 'json-binary-roundtrip',
                                 'unique-error-rollback', 'copy-protocol',
                                 'certificate-hostname-rejection'],
                      'driver': psycopg2.__version__, 'ssl': ssl.OPENSSL_VERSION}))


if __name__ == '__main__':
    main()
