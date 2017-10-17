""" This module contains data dump creation and import functions.
"""

# listenbrainz-server - Server for the ListenBrainz project
#
# Copyright (C) 2017 MetaBrainz Foundation Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.


import listenbrainz.config as config
import listenbrainz.db as db
import logging
import os
import shutil
import sqlalchemy
import subprocess
import sys
import tarfile
import tempfile

from datetime import datetime
from listenbrainz.utils import create_path, log_ioerrors


DUMP_LICENSE_FILE_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                      "licenses", "COPYING-PublicDomain")

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# this dict contains the tables dumped in stats dump as keys
# and a tuple of columns that should be dumped as values
STATS_TABLES = {
    '"user"': (
        'id',
        'created',
        'musicbrainz_id',
        # the following are dummy values for columns that we do not want to
        # dump in the stats dump
        '\'auth_token\'', # auth token
        'to_timestamp(0)', # last_login
        'to_timestamp(0)', # latest_import
    ),
    'statistics.user': (
        'user_id',
        'artist',
        'release',
        'recording',
        'last_updated',
    ),
    'statistics.artist': (
        'id',
        'msid',
        'name',
        'release',
        'recording',
        'listener',
        'listen_count',
        'last_updated',
    ),
    'statistics.release': (
        'id',
        'msid',
        'name',
        'recording',
        'listener',
        'listen_count',
        'last_updated',
    ),
    'statistics.recording': (
        'id',
        'msid',
        'name',
        'listener',
        'listen_count',
        'last_updated',
    ),
}

# this dict contains the tables dumped in the private dump as keys
# and a tuple of columns that should be dumped as values
PRIVATE_TABLES = {
    '"user"': (
        'id',
        'created',
        'musicbrainz_id',
        'auth_token',
        'last_login',
        'latest_import',
    ),
    'api_compat.token': (
        'id',
        'user_id',
        'token',
        'api_key',
        'ts',
    ),
    'api_compat.session': (
        'id',
        'user_id',
        'sid',
        'api_key',
        'ts',
    ),
}


def dump_postgres_db(location, threads=None):
    """ Create postgres database dump in the specified location

        Arguments:
            location: Directory where the final dump will be stored
            threads: Maximal number of threads to run during compression

        Returns:
            Path to created dump.
    """

    logger.info('Beginning dump of PostgreSQL database...')
    time_now = datetime.today()
    dump_path = os.path.join(location, 'listenbrainz-dump-{time}'.format(time=time_now.strftime('%Y%m%d-%H%M%S')))
    create_path(dump_path)
    logger.info('dump path: %s', dump_path)


    logger.info('Creating dump of private data...')
    try:
        private_dump = create_private_dump(dump_path, time_now, threads)
    except IOError as e:
        log_ioerrors(logger, e)
        logger.info('Removing created files and giving up...')
        shutil.rmtree(dump_path)
        return
    except Exception as e:
        logger.error('Unable to create private db dump due to error %s', str(e))
        logger.info('Removing created files and giving up...')
        shutil.rmtree(dump_path)
        return
    logger.info('Dump of private data created at %s!', private_dump)

    logger.info('Creating dump of stats data...')
    try:
        stats_dump = create_stats_dump(dump_path, time_now, threads)
    except IOError as e:
        log_ioerrors(logger, e)
        logger.info('Removing created files and giving up...')
        shutil.rmtree(dump_path)
        return
    except Exception as e:
        logger.error('Unable to create statistics dump due to error %s', str(e))
        logger.info('Removing created files and giving up...')
        shutil.rmtree(dump_path)
        return
    logger.info('Dump of stats data created at %s!', stats_dump)


    logger.info('Creating a new entry in the data_dump table...')
    while True:
        try:
            dump_id = add_dump_entry()
            break
        except Exception as e:
            logger.error('Error while adding dump entry: %s', str(e))

    logger.info('New entry with id %d added to data_dump table!', dump_id)

    logger.info('ListenBrainz PostgreSQL data dump created at %s!', dump_path)
    return dump_path


def _create_dump(location, dump_type, tables, time_now, threads=None):
    """ Creates a dump of the provided tables at the location passed

        Arguments:
            location: the path where the dump should be created
            dump_type: the type of data dump being made - private or stats
            tables: a dict containing the names of the tables to be dumped as keys and the columns
                    to be dumped as values
            time_now: the time at which the dump process was started
            threads: the maximum number of threads to use for compression

        Returns:
            the path to the archive file created
    """

    archive_name = 'listenbrainz-{dump_type}-dump-{time}'.format(
        dump_type=dump_type,
        time=time_now.strftime('%Y%m%d-%H%M%S')
    )
    archive_path = os.path.join(location, '{archive_name}.tar.xz'.format(
        archive_name=archive_name,
    ))

    with open(archive_path, 'w') as archive:

        pxz_command = ['pxz', '--compress']
        if threads is not None:
            pxz_command.append('-T {threads}'.format(threads=threads))

        pxz = subprocess.Popen(pxz_command, stdin=subprocess.PIPE, stdout=archive)

        with tarfile.open(fileobj=pxz.stdin, mode='w|') as tar:

            temp_dir = tempfile.mkdtemp()

            try:
                schema_seq_path = os.path.join(temp_dir, "SCHEMA_SEQUENCE")
                with open(schema_seq_path, "w") as f:
                    f.write(str(db.SCHEMA_VERSION))
                tar.add(schema_seq_path,
                        arcname=os.path.join(archive_name, "SCHEMA_SEQUENCE"))
                timestamp_path = os.path.join(temp_dir, "TIMESTAMP")
                with open(timestamp_path, "w") as f:
                    f.write(time_now.isoformat(" "))
                tar.add(timestamp_path,
                        arcname=os.path.join(archive_name, "TIMESTAMP"))
                tar.add(DUMP_LICENSE_FILE_PATH,
                        arcname=os.path.join(archive_name, "COPYING"))
            except IOError as e:
                logger.error('IOError while adding dump metadata...')
                raise
            except Exception as e:
                logger.error('Exception while adding dump metadata: %s', str(e))
                raise


            archive_tables_dir = os.path.join(temp_dir, 'lbdump', 'lbdump')
            create_path(archive_tables_dir)


            with db.engine.connect() as connection:
                with connection.begin() as transaction:
                    cursor = connection.connection.cursor()
                    for table in tables:
                        try:
                            copy_table(
                                cursor=cursor,
                                location=archive_tables_dir,
                                columns=','.join(tables[table]),
                                table_name=table,
                            )
                        except IOError as e:
                            logger.error('IOError while copying table %s', table)
                            raise
                        except Exception as e:
                            logger.error('Error while copying table %s: %s', table, str(e))
                            raise
                    transaction.rollback()


            tar.add(archive_tables_dir, arcname=os.path.join(archive_name, 'lbdump'.format(dump_type)))

            shutil.rmtree(temp_dir)

        pxz.stdin.close()

    return location


def create_private_dump(location, time_now, threads=None):
    """ Create postgres database dump for private data in db.
        This includes dumps of the following tables:
            "user",
            api_compat.token,
            api_compat.session
    """
    return _create_dump(
        location=location,
        dump_type='private',
        tables=PRIVATE_TABLES,
        time_now=time_now,
        threads=threads,
    )


def create_stats_dump(location, time_now, threads=None):
    """ Create postgres database dump for statistics in db.
        This includes dumps of all tables in the statistics schema:
            statistics.user
            statistics.artist
            statistics.release
            statistics.recording
    """
    return _create_dump(
        location=location,
        dump_type='stats',
        tables=STATS_TABLES,
        time_now=time_now,
        threads=threads,
    )


def copy_table(cursor, location, columns, table_name):
    """ Copies a PostgreSQL table to a file

        Arguments:
            cursor: a psycopg cursor
            location: the directory where the table should be copied
            columns: a comma seperated string listing the columns of the table
                     that should be dumped
            table_name: the name of the table to be copied
    """

    with open(os.path.join(location, table_name), 'w') as f:
        cursor.copy_to(f, '(SELECT {columns} FROM {table})'.format(
            columns=columns,
            table=table_name
        ))


def add_dump_entry():
    """ Adds an entry to the data_dump table with current time.

        Returns:
            id (int): the id of the new entry added
    """

    with db.engine.connect() as connection:
        result = connection.execute(sqlalchemy.text("""
                INSERT INTO data_dump (created)
                     VALUES (NOW())
                  RETURNING id
            """))
        return result.fetchone()['id']


def import_postgres_dump(location):

    private_dump_archive_path = None
    stats_dump_archive_path = None

    for archive in os.listdir(location):
        if os.path.isfile(os.path.join(location, archive)):
            if 'private' in archive:
                private_dump_archive_path = os.path.join(location, archive)
            else:
                stats_dump_archive_path = os.path.join(location, archive)


    if private_dump_archive_path:
        logger.info('Importing private dump %s...', private_dump_archive_path)
        try:
            _import_dump(private_dump_archive_path, 'private', PRIVATE_TABLES)
            logger.info('Import of private dump %s done!', private_dump_archive_path)
        except SchemaMismatchException as e:
            logger.error(str(e))


    if stats_dump_archive_path:
        logger.info('Importing stats dump %s...', stats_dump_archive_path)
        try:
            _import_dump(stats_dump_archive_path, 'private', PRIVATE_TABLES)
            logger.info('Import of stats dump %s done!', stats_dump_archive_path)
        except SchemaMismatchException as e:
            logger.error(str(e))


def _import_dump(archive_path, dump_type, tables):

    pxz_command = ["pxz", "--decompress", "--stdout", archive_path]
    pxz = subprocess.Popen(pxz_command, stdout=subprocess.PIPE)

    table_names = STATS_TABLES if dump_type == 'stats' else PRIVATE_TABLES

    connection = db.engine.raw_connection()
    try:
        cursor = connection.cursor()

        with tarfile.open(fileobj=pxz.stdout, mode="r|") as tar:
            for member in tar:
                file_name = member.name.split("/")[-1]

                if file_name == "SCHEMA_SEQUENCE":
                    # Verifying schema version
                    schema_seq = int(tar.extractfile(member).read().strip())
                    if schema_seq != db.SCHEMA_VERSION:
                        raise SchemaMismatchException("Incorrect schema version! Expected: %d, got: %d."
                                        "Please, get the latest version of the dump."
                                        % (db.SCHEMA_VERSION, schema_seq))
                    else:
                        logging.info("Schema version verified.")

                else:
                    if file_name in table_names:
                        logging.info(" - Importing data into %s table..." % file_name)
                        cursor.copy_from(tar.extractfile(member), '%s' % file_name,
                                         columns=TABLES[file_name])
        connection.commit()
    finally:
        connection.close()

    pxz.stdout.close()


class SchemaMismatchException(Exception):
    pass
