# *********************************************************************************************************************
# load-gnaf.py
# *********************************************************************************************************************
#
# A script for loading raw GNAF & PSMA Admin boundaries and creating flattened, complete, easy to use versions of them
#
# Author: Hugh Saalmans
# GitHub: minus34
# Twitter: @minus34
#
# Version: 1.0.0
# Date: 22-02-2016
#
# Process:
#   1. Loads raw GNAF into Postgres from PSV files using COPY
#   2. Loads raw PSMA Admin Boundaries from Shapefiles into Postgres using shp2pgsql (part of PostGIS)
#   3. Creates flattened and simplified GNAF tables containing all relevant data
#   4. Creates a ready to use Locality Boundaries table containing a number of fixes to overcome known data issues
#   5. Splits the locality boundary for Melbourne into 2, one for each of its postcodes (3000 & 3004)
#   6. Creates final principal & alias address tables containing fixes based on the above locality customisations
#   7. Creates an almost correct Postcode Boundary table from locality boundary aggregates with address based postcodes
#   8. Adds primary and foreign keys to check for PID integrity across the reference tables
#
# TO DO:
# - create ready-to-use versions of all admin bdys
# - boundary tag addresses for census bdys
# - boundary tag addresses for admin bdys
# - output reference tables to PSV & SHP
# - check address_alias_lookup record count
# - QA against alternative method for flattening GNAF
#
# *********************************************************************************************************************

import multiprocessing
import math
import os
import subprocess
import platform
import psycopg2

from datetime import datetime

# *********************************************************************************************************************
# Edit these parameters to taste - START
# *********************************************************************************************************************

# vacuum database at the start after dropping tables?
vacuum_db = True

# create primary & foreign keys for raw gnaf? (adds time to data load)
# NOTE: final reference tables will have PKs & FKs for data integrity
primary_foreign_keys = False

# create unlogged raw gnaf tables
# (only set to True if you don't care about the raw data afterwards - they will be lost if the server crashes!)
unlogged_tables = False

# which states do you want to load the gnaf data for?
states_to_load = ["ACT", "NSW", "NT", "OT", "QLD", "SA", "TAS", "VIC", "WA"]
# states_to_load = ["ACT", "NSW"]

# what are the maximum parallel processes you want to use for the data load?
# (set it to the number of cores on the Postgres server minus 2, limit to 12 if 16+ cores - minimal benefit beyond 12)
max_concurrent_processes = 6

# Postgres parameters
pg_host = "localhost"
pg_port = 5433
pg_db = "gnaf_test2"
pg_user = "postgres"
pg_password = "password"

# schema names for the raw gnaf, flattened reference and admin boundary tables
raw_gnaf_schema = "raw_gnaf"
raw_admin_bdys_schema = "raw_admin_bdys"
gnaf_schema = "gnaf"
admin_bdys_schema = "admin_bdys"

# raw data directories
# gnaf_network_directory = r"\\l10-geosdi\h$\zzz_todelete"
# gnaf_pg_server_local_directory = r"h:\zzz_todelete"
# admin_bdys_local_directory = r"C:\temp\psma_201511"
# # psv_output_directory = r"C:\temp"

gnaf_network_directory = r"C:\temp\psma_201511"
gnaf_pg_server_local_directory = r"C:\temp\psma_201511"
admin_bdys_local_directory = r"C:\temp\psma_201511"
# psv_output_directory = r"C:\temp"

# gnaf_network_directory = r"C:\temp\psma_201511"
# gnaf_pg_server_local_directory = "/home/vagrant/sync"
# admin_bdys_local_directory = r"C:\temp\psma_201511"
# psv_output_directory = r"C:\temp"

# gnaf_network_directory = "/Users/Hugh/tmp"
# gnaf_pg_server_local_directory = "/Users/Hugh/tmp"
# admin_bdys_local_directory = "/Users/Hugh/tmp"
# psv_output_directory = "/Users/Hugh/tmp"

# *********************************************************************************************************************
# Edit these parameters to taste - END
# *********************************************************************************************************************

# create postgres connect string
pg_connect_string = "dbname='{0}' host='{1}' port='{2}' user='{3}' password='{4}'"\
    .format(pg_db, pg_host, pg_port, pg_user, pg_password)

# set postgres script directory
if platform.system() == "Windows":
    sql_dir = os.path.dirname(os.path.realpath(__file__)) + "\\postgres-scripts\\"
else:  # assume all else use forward slashes
    sql_dir = os.path.dirname(os.path.realpath(__file__)) + "/postgres-scripts/"


def main():
    full_start_time = datetime.now()

    # connect to Postgres
    try:
        pg_conn = psycopg2.connect(pg_connect_string)
    except psycopg2.Error:
        print "Unable to connect to database\nACTION: Check your Postgres parameters and/or database security"
        return False

    pg_conn.autocommit = True
    pg_cur = pg_conn.cursor()

    # add postgis to database (in the public schema) - run this in a try first time to confirm db user has privileges
    try:
        pg_cur.execute("SET search_path = public, pg_catalog; CREATE EXTENSION IF NOT EXISTS postgis")
    except psycopg2.Error:
        print "Unable to add PostGIS extension\nACTION: Check your Postgres user privileges or PostGIS install"
        return False

    # PART 1 - load gnaf from PSV files
    print ""
    start_time = datetime.now()
    print "Part 1 of 3 : Start raw GNAF load : {0}".format(start_time)
    drop_tables_and_vacuum_db(pg_cur)
    create_raw_gnaf_tables(pg_cur)
    populate_raw_gnaf()
    index_raw_gnaf()
    if primary_foreign_keys:
        create_primary_foreign_keys()
    else:
        print "\t- Step 6 of 6 : primary & foreign keys NOT created"
    # set postgres search path back to the default
    pg_cur.execute("SET search_path = public, pg_catalog")
    print "Part 1 of 3 : Raw GNAF loaded! : {0}".format(datetime.now() - start_time)
    
    # PART 2 - load raw admin boundaries from Shapefiles
    print ""
    start_time = datetime.now()
    print "Part 2 of 3 : Start raw admin boundary load : {0}".format(start_time)
    if load_admin_boundaries(pg_cur):
        print "Part 2 of 3 : Raw admin boundaries loaded! : {0}".format(datetime.now() - start_time)
    else:
        print "Part 2 of 3 : Raw admin boundaries load FAILED!"

    # PART 3 - create flattened and standardised GNAF and Administrative Boundary reference tables
    print ""
    start_time = datetime.now()
    print "Part 3 of 3 : Start create reference tables : {0}".format(start_time)
    create_reference_tables(pg_cur)
    print "Part 3 of 3 : Reference tables created! : {0}".format(datetime.now() - start_time)

    pg_cur.close()
    pg_conn.close()

    print "Total time : : {0}".format(datetime.now() - full_start_time)


def drop_tables_and_vacuum_db(pg_cur):
    # Step 1 of 6 : drop tables
    start_time = datetime.now()
    pg_cur.execute(open_sql_file("01-01-drop-tables.sql"))
    print "\t- Step 1 of 6 : tables dropped : {0}".format(datetime.now() - start_time)

    # Step 2 of 6 : vacuum database (if requested)
    start_time = datetime.now()
    if vacuum_db:
        pg_cur.execute("VACUUM")
        print "\t- Step 2 of 6 : database vacuumed : {0}".format(datetime.now() - start_time)
    else:
        print "\t- Step 2 of 6 : database NOT vacuumed : {0}"


def create_raw_gnaf_tables(pg_cur):
    # Step 3 of 6 : create tables
    start_time = datetime.now()

    # prep create table sql scripts (note: file doesn't contain any schema prefixes on table names)
    sql = open(sql_dir + "01-03-raw-gnaf-create-tables.sql", "r").read()

    # create schema and set as search path
    if raw_gnaf_schema != "public":
        pg_cur.execute("CREATE SCHEMA IF NOT EXISTS {0} AUTHORIZATION {1}".format(raw_gnaf_schema, pg_user))
        pg_cur.execute("SET search_path = {0}".format(raw_gnaf_schema,))

        # alter create table script to run on chosen schema
        sql = sql.replace("SET search_path = public", "SET search_path = {0}".format(raw_gnaf_schema,))

    # set tables to unlogged to speed up the load? (if requested)
    # -- they'll have to be rebuilt using this script again after a system crash --
    if unlogged_tables:
        sql = sql.replace("CREATE TABLE ", "CREATE UNLOGGED TABLE ")
        unlogged_string = "UNLOGGED "
    else:
        unlogged_string = ""

    # create raw gnaf tables
    pg_cur.execute(sql)

    print "\t- Step 3 of 6 : {1}tables created : {0}".format(datetime.now() - start_time, unlogged_string)


# load raw gnaf authority & state tables using multiprocessing
def populate_raw_gnaf():
    # Step 4 of 6 : load raw gnaf authority & state tables
    start_time = datetime.now()

    # authority code file list
    sql_list = get_raw_gnaf_files("authority_code")

    # add state file lists
    for state in states_to_load:
        sql_list.extend(get_raw_gnaf_files(state))

    # are there any files to load?
    if len(sql_list) == 0:
        print "No raw GNAF PSV files found\nACTION: Check your 'gnaf_network_directory' path"
        print "\t- Step 4 of 6 : table populate FAILED!"
    else:
        # load all PSV files using multiprocessing
        multiprocess_list(max_concurrent_processes, "sql", sql_list)
        print "\t- Step 4 of 6 : tables populated : {0}".format(datetime.now() - start_time)


def get_raw_gnaf_files(prefix):
    sql_list = []
    prefix = prefix.lower()
    # get a dictionary of all files matching the filename prefix
    for root, dirs, files in os.walk(gnaf_network_directory):
        for file_name in files:
            if file_name.lower().startswith(prefix + "_"):
                if file_name.lower().endswith(".psv"):
                    file_path = os.path.join(root, file_name)\
                        .replace(gnaf_network_directory, gnaf_pg_server_local_directory)
                    table = file_name.lower().replace(prefix + "_", "", 1).replace("_psv.psv", "")

                    # if a non-Windows Postgres server OS - fix file path
                    if gnaf_pg_server_local_directory[0:1] == "/":
                        file_path = file_path.replace("\\", "/")
                        # print file_path

                    sql = "COPY {0}.{1} FROM '{2}' DELIMITER '|' CSV HEADER;".format(raw_gnaf_schema, table, file_path)

                    sql_list.append(sql)

    return sql_list


# index raw gnaf using multiprocessing
def index_raw_gnaf():
    # Step 5 of 6 : create indexes
    start_time = datetime.now()

    raw_sql_list = open_sql_file("01-05-raw-gnaf-create-indexes.sql").split("\n")
    sql_list = []
    for sql in raw_sql_list:
        if sql[0:2] != "--" and sql[0:2] != "":
            sql_list.append(sql)

    multiprocess_list(max_concurrent_processes, "sql", sql_list)
    print "\t- Step 5 of 6 : indexes created: {0}".format(datetime.now() - start_time)


# create raw gnaf primary & foreign keys (for data integrity) using multiprocessing
def create_primary_foreign_keys():
    start_time = datetime.now()

    key_sql = open(sql_dir + "01-06-raw-gnaf-create-primary-foreign-keys.sql", "r").read()
    key_sql_list = key_sql.split("--")
    sql_list = []

    for sql in key_sql_list:
        sql = sql.strip()
        if sql[0:6] == "ALTER ":
            # add schema to tables names, in case raw gnaf schema not the default
            sql = sql.replace("ALTER TABLE ONLY ", "ALTER TABLE ONLY " + raw_gnaf_schema + ".")
            sql_list.append(sql)

    # run queries in separate processes
    multiprocess_list(max_concurrent_processes, "sql", sql_list)

    print "\t- Step 6 of 6 : primary & foreign keys created : {0}".format(datetime.now() - start_time)


# loads the admin bdy shapefiles using the shp2pgsql command line tool (part of PostGIS), using multiprocessing
def load_admin_boundaries(pg_cur):
    # create schema
    if raw_admin_bdys_schema != "public":
        pg_cur.execute("CREATE SCHEMA IF NOT EXISTS {0} AUTHORIZATION {1}".format(raw_admin_bdys_schema, pg_user))

    # set psql connect string and password
    psql_str = "psql -U {0} -d {1} -h {2} -p {3}".format(pg_user, pg_db, pg_host, pg_port)
    if platform.system() == "Windows":
        password_str = "SET"
    else:
        password_str = "export"

    password_str += " PGPASSWORD={0}&&".format(pg_password)

    # get file list
    table_list = []
    cmd_list = []
    for state in states_to_load:
        state = state.lower()
        # get a dictionary of Shapefiles and DBFs matching the state
        for root, dirs, files in os.walk(admin_bdys_local_directory):
            for file_name in files:
                if file_name.lower().startswith(state + "_"):
                    if file_name.lower().endswith("_shp.dbf"):
                        # change file type for spatial files
                        if file_name.lower().endswith("_polygon_shp.dbf"):
                            spatial = True
                            bdy_file = os.path.join(root, file_name.replace(".dbf", ".shp"))
                        else:
                            spatial = False
                            bdy_file = os.path.join(root, file_name)

                        bdy_table = file_name.lower().replace(state + "_", "aus_", 1).replace("_shp.dbf", "")

                        # set command line parameters depending on whether this is the 1st state (for creating tables)
                        if bdy_table not in table_list:
                            table_list.append(bdy_table)

                            if spatial:
                                params = "-d -D -s 4283 -i"
                            else:
                                params = "-d -D -G -n -i"
                        else:
                            if spatial:
                                params = "-a -D -s 4283 -i"
                            else:
                                params = "-a -D -G -n -i"

                        cmd = "{0}shp2pgsql {1} \"{2}\" {3}.{4} | {5}"\
                            .format(password_str, params, bdy_file, raw_admin_bdys_schema, bdy_table, psql_str)

                        # if locality file from Towns folder: don't add - it's a duplicate
                        if "town points" not in bdy_file.lower():
                            cmd_list.append(cmd)
                        else:
                            if not bdy_file.lower().endswith("_locality_shp.dbf"):
                                cmd_list.append(cmd)

    # are there any files to load?
    if len(cmd_list) == 0:
        print "No Admin Boundary files found\nACTION: Check your 'admin_bdys_local_directory' path"
        return False
    else:
        # load files in separate processes
        multiprocess_list(max_concurrent_processes, "cmd", cmd_list)
        return True


# create gnaf reference tables by flattening raw gnaf address, streets & localities into a usable form
# also creates all supporting lookup tables and usable admin bdy tables
def create_reference_tables(pg_cur):
    # set postgres search path back to the default
    pg_cur.execute("SET search_path = public, pg_catalog")

    # create schemas
    if gnaf_schema != "public":
        pg_cur.execute("CREATE SCHEMA IF NOT EXISTS {0} AUTHORIZATION {1}".format(gnaf_schema, pg_user))
    if admin_bdys_schema != "public":
        pg_cur.execute("CREATE SCHEMA IF NOT EXISTS {0} AUTHORIZATION {1}"
                       .format(admin_bdys_schema, pg_user))

    # Step 1 of 15 : create reference tables
    start_time = datetime.now()
    pg_cur.execute(open_sql_file("03-01-reference-create-tables.sql"))
    print "\t- Step  1 of 15 : create reference tables : {0}".format(datetime.now() - start_time)

    # Step 2 of 15 : populate localities
    start_time = datetime.now()
    pg_cur.execute(open_sql_file("03-02-reference-populate-localities.sql"))
    print "\t- Step  2 of 15 : localities populated : {0}".format(datetime.now() - start_time)

    # Step 3 of 15 : populate locality aliases
    start_time = datetime.now()
    pg_cur.execute(open_sql_file("03-03-reference-populate-locality-aliases.sql"))
    print "\t- Step  3 of 15 : locality aliases populated : {0}".format(datetime.now() - start_time)

    # Step 4 of 15 : populate locality neighbours
    start_time = datetime.now()
    pg_cur.execute(open_sql_file("03-04-reference-populate-locality-neighbours.sql"))
    print "\t- Step  4 of 15 : locality neighbours populated : {0}".format(datetime.now() - start_time)

    # Step 5 of 15 : populate streets
    start_time = datetime.now()
    pg_cur.execute(open_sql_file("03-05-reference-populate-streets.sql"))
    print "\t- Step  5 of 15 : streets populated : {0}".format(datetime.now() - start_time)

    # Step 6 of 15 : populate street aliases
    start_time = datetime.now()
    pg_cur.execute(open_sql_file("03-06-reference-populate-street-aliases.sql"))
    print "\t- Step  6 of 15 : street aliases populated : {0}".format(datetime.now() - start_time)

    # Step 7 of 15 : populate addresses, using multiprocessing
    start_time = datetime.now()
    sql = open_sql_file("03-07-reference-populate-addresses-1.sql")
    split_sql_into_list_and_process(pg_cur, sql, gnaf_schema, "streets", "str", "gid")
    pg_cur.execute(prep_sql("ANALYZE gnaf.temp_addresses;"))
    print "\t- Step  7 of 15 : addresses populated : {0}".format(datetime.now() - start_time)

    # Step 8 of 15 : populate principal alias lookup
    start_time = datetime.now()
    pg_cur.execute(open_sql_file("03-08-reference-populate-address-alias-lookup.sql"))
    print "\t- Step  8 of 15 : principal alias lookup populated : {0}".format(datetime.now() - start_time)

    # Step 9 of 15 : populate primary secondary lookup
    start_time = datetime.now()
    pg_cur.execute(open_sql_file("03-09-reference-populate-address-secondary-lookup.sql"))
    pg_cur.execute(prep_sql("VACUUM ANALYSE gnaf.address_secondary_lookup"))
    print "\t- Step  9 of 15 : primary secondary lookup populated : {0}".format(datetime.now() - start_time)

    # Step 10 of 15 : populate locality boundaries
    start_time = datetime.now()
    pg_cur.execute(open_sql_file("03-10-reference-create-locality-bdys.sql"))
    print "\t- Step 10 of 15 : locality boundaries populated : {0}".format(datetime.now() - start_time)

    # Step 11 of 15 : split the Melbourne locality into its 2 postcodes (3000, 3004)
    start_time = datetime.now()
    pg_cur.execute(open_sql_file("03-11-reference-split-melbourne.sql"))
    print "\t- Step 11 of 15 : Melbourne split : {0}".format(datetime.now() - start_time)

    # Step 12 of 15 : finalise localities assigned to streets and addresses
    start_time = datetime.now()
    pg_cur.execute(open_sql_file("03-12-reference-finalise-localities.sql"))
    # pg_cur.execute(prep_sql("VACUUM ANALYZE gnaf.localities"))
    print "\t- Step 12 of 15 : localities finalised : {0}".format(datetime.now() - start_time)

    # Step 13 of 15 : finalise addresses, using multiprocessing
    start_time = datetime.now()
    sql = open_sql_file("03-13-reference-populate-addresses-2.sql")
    split_sql_into_list_and_process(pg_cur, sql, gnaf_schema, "localities", "loc", "gid")
    # turf the temp address table
    # pg_cur.execute(prep_sql("DROP TABLE IF EXISTS gnaf.temp_addresses"))
    print "\t- Step 13 of 15 : addresses finalised : {0}".format(datetime.now() - start_time)

    # Step 14 of 15 : create almost correct postcode boundaries by aggregating localities, using multiprocessing
    start_time = datetime.now()
    sql = open_sql_file("03-14-reference-derived-postcode-bdys.sql")
    sql_list = []
    for state in states_to_load:
        state_sql = sql.replace("GROUP BY ", "WHERE state = '{0}' GROUP BY ".format(state))
        sql_list.append(state_sql)
    multiprocess_list(max_concurrent_processes, "sql", sql_list)
    print "\t- Step 14 of 15 : postcode boundaries created : {0}".format(datetime.now() - start_time)

    # Step 15 of 15 : create indexes, primary and foreign keys, using multiprocessing
    start_time = datetime.now()
    raw_sql_list = open_sql_file("03-15-reference-create-indexes.sql").split("\n")
    sql_list = []
    for sql in raw_sql_list:
        if sql[0:2] != "--" and sql[0:2] != "":
            sql_list.append(sql)
    multiprocess_list(max_concurrent_processes, "sql", sql_list)
    print "\t- Step 14 of 15 : create indexes : {0}".format(datetime.now() - start_time)


# takes a list of sql queries or command lines and runs them using multiprocessing
def multiprocess_list(concurrent_processes, mp_type, work_list):
    pool = multiprocessing.Pool(processes=concurrent_processes)

    if mp_type == "sql":
        results = pool.imap_unordered(run_sql_multiprocessing, work_list)
    else:
        results = pool.imap_unordered(run_command_line, work_list)

    pool.close()
    pool.join()

    for result in results:
        if result is not None:
            print result


def run_sql_multiprocessing(the_sql):
    pg_conn = psycopg2.connect(pg_connect_string)
    pg_conn.autocommit = True
    pg_cur = pg_conn.cursor()

    # set raw gnaf database schema (it's needed for the primary and foreign key creation)
    if raw_gnaf_schema != "public":
        pg_cur.execute("SET search_path = {0}, public, pg_catalog".format(raw_gnaf_schema,))

    try:
        pg_cur.execute(the_sql)
    except psycopg2.Error, e:
        return "SQL FAILED! : {0} : {1}".format(the_sql, e.message)

    pg_cur.close()
    pg_conn.close()

    return None


def run_command_line(cmd):
    # run the command line without any output (it'll still tell you if it fails)
    try:
        fnull = open(os.devnull, "w")
        subprocess.call(cmd, shell=True, stdout=fnull, stderr=subprocess.STDOUT)
    except Exception, e:
        return "COMMAND FAILED! : {0} : {1}".format(cmd, e.message)

    return None


def open_sql_file(file_name):
    sql = open(sql_dir + file_name, "r").read()
    return prep_sql(sql)


# change schema names in an array of SQL script if schemas not the default
def prep_sql_list(sql_list):
    output_list = []
    for sql in sql_list:
        output_list.append(prep_sql(sql))
    return output_list


# change schema names in the SQL script if not the default
def prep_sql(sql):
    if raw_gnaf_schema != "raw_gnaf":
        sql = sql.replace(" raw_gnaf.", " {0}.".format(raw_gnaf_schema,))
    if gnaf_schema != "gnaf":
        sql = sql.replace(" gnaf.", " {0}.".format(gnaf_schema,))
    if raw_admin_bdys_schema != "raw_admin_bdys":
        sql = sql.replace(" raw_admin_bdys.", " {0}.".format(raw_admin_bdys_schema,))
    if admin_bdys_schema != "admin_bdys":
        sql = sql.replace(" admin_bdys.", " {0}.".format(admin_bdys_schema,))
    return sql


def split_sql_into_list_and_process(pg_cur, the_sql, table_schema, table_name, table_alias, table_gid):
    # get min max gid values from the table to split
    min_max_sql = "SELECT MIN({2}) AS min, MAX({2}) AS max FROM {0}.{1}".format(table_schema, table_name, table_gid)

    pg_cur.execute(min_max_sql)
    result = pg_cur.fetchone()

    min_pkey = int(result[0])
    max_pkey = int(result[1])
    diff = max_pkey - min_pkey

    # Number of records in each query
    rows_per_request = int(math.floor(float(diff) / float(max_concurrent_processes))) + 1

    # If less records than processes or rows per request, reduce both to allow for a minimum of 15 records each process
    if float(diff) / float(max_concurrent_processes) < 10.0:
        rows_per_request = 10
        processes = int(math.floor(float(diff) / 10.0)) + 1
        print "\t\t- running {0} processes (adjusted due to low row count in table to split)".format(processes)
    else:
        processes = max_concurrent_processes
        # print "\t\t- running {0} processes".format(processes)

    # create list of sql statements to run with multiprocessing
    sql_list = []
    start_pkey = min_pkey - 1

    for i in range(0, processes):
        end_pkey = start_pkey + rows_per_request

        where_clause = " WHERE {0}.{3} > {1} AND {0}.{3} <= {2}".format(table_alias, start_pkey, end_pkey, table_gid)

        if "WHERE " in the_sql:
            mp_sql = the_sql.replace(" WHERE ", where_clause + " AND ")
        elif "GROUP BY " in the_sql:
            mp_sql = the_sql.replace("GROUP BY ", where_clause + " GROUP BY ")
        elif "ORDER BY " in the_sql:
            mp_sql = the_sql.replace("ORDER BY ", where_clause + " ORDER BY ")
        else:
            mp_sql = the_sql.replace(";", where_clause + ";")

        sql_list.append(mp_sql)
        start_pkey = end_pkey

    # print '\n'.join(sql_list)
    multiprocess_list(processes, 'sql', sql_list)


if __name__ == '__main__':
    main()
