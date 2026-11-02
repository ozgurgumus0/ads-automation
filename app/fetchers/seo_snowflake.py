"""
Organic lead counts from Snowflake for fetchers/seo.py (the report's lead source).

Runs on its own under Python 3.13 (`py -3.13 seo_snowflake.py START END`): the
Snowflake connector isn't supported on the app's Python 3.14. Prints one JSON
line: [[date, entrance host/path, productType, leads], ...].
`--check` only logs in and prints user/role (the SEO page's Snowflake button).

Organic = CHANNEL 'organic', minus leads that entered on example.org (the manual
report's rule). Counts only: no column about a person is ever selected.
Login = Snowflake SSO in the browser; the token is cached by the connector.
"""

import json
import os
import sys

import snowflake.connector
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

SQL = """
select createdat_date::string,
       lower(split_part(split_part(regexp_replace(coalesce(entranceurl, ''), '^https?://', ''), '?', 1), '#', 1)),
       producttype,
       count(*)
from {LEAD_TABLE}
where createdat_date between %(start)s and %(end)s
  and lower(channel) = 'organic'
  and coalesce(lower(entranceurl), '') not like '%%{EXCLUDED_HOST}%%'
group by 1, 2, 3
"""
SQL = (SQL.replace("{LEAD_TABLE}", os.getenv("SNOWFLAKE_DATABASE", "ANALYTICS") + "."
                   + os.getenv("SNOWFLAKE_SCHEMA", "RAW") + "." + os.getenv("SNOWFLAKE_TABLE_LEAD", "LEAD"))
          .replace("{EXCLUDED_HOST}", os.getenv("SEO_EXCLUDED_HOST", "example.org")))


def connect():
    p = {k: os.getenv(f"SNOWFLAKE_{k.upper()}") for k in ("account", "user", "role", "warehouse", "database")}
    return snowflake.connector.connect(authenticator="externalbrowser", client_store_temporary_credential=True,
                                       **{k: v for k, v in p.items() if v})


def check():
    """Log in (opens the browser if the cached token expired) and print who we are."""
    con = connect()
    try:
        cur = con.cursor()
        cur.execute("select current_user(), current_role(), current_warehouse()")
        u, r, w = cur.fetchone()
        print(json.dumps({"user": u, "role": r, "warehouse": w}))
    finally:
        con.close()


def main(start, end):
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(SQL, {"start": start, "end": end})
        print(json.dumps([[d, url, prod, int(n)] for d, url, prod, n in cur.fetchall()]))
    finally:
        con.close()


if __name__ == "__main__":
    check() if sys.argv[1:] == ["--check"] else main(sys.argv[1], sys.argv[2])
