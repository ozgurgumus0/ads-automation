"""
Creative feed, rebuilt: the creative dashboard's cohort funnel (New_Data) and
lead -> contract cohort (Lead_Contract_Cohort) straight from Snowflake, for
when the data team's own export package (creative_feed/scripts/metrics) isn't available.

Rebuilt from the team's written funnel definitions and calibrated against the
data team's own feed files. Calibration found three things the rule text
doesn't say:
  - affiliation / criteo / mediago leads have an EMPTY channel (not Organic)
  - a contract on a MANUAL lead moves to the subject's callable lead chosen
    among the report's own cohort (CALLABLE first, then lowest id)
  - dates are LEAD.CREATEDAT_DATE (UTC)

Runs under Python 3.13 (Snowflake connector): py -3.13 creative_snowflake.py START END OUT.json
Writes {"nd": [...], "lcc": [...]} - counts only, grouped; never a row per person.
"""

import json
import os
import sys

import snowflake.connector
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# Fully-qualified table names: SNOWFLAKE_DATABASE / SNOWFLAKE_SCHEMA plus one env
# var per table (SNOWFLAKE_TABLE_<KEY>) so the query maps onto your own warehouse.
_DB = os.getenv("SNOWFLAKE_DATABASE", "ANALYTICS")
_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", "RAW")
TABLES = {key: f"{_DB}.{_SCHEMA}." + os.getenv(f"SNOWFLAKE_TABLE_{key}", default)
          for key, default in (("LEAD", "LEAD"), ("CALENDAR_EVENT", "CALENDAR_EVENT"), ("REQUEST", "REQUEST"),
                               ("FINAL_SURVEY", "FINAL_SURVEY"), ("CONTRACTS", "CONTRACTS"),
                               ("LEAD_TIME", "LEAD_TIME"))}
CONTRACT_ORG = os.getenv("SNOWFLAKE_CONTRACT_ORG", "COMPANY")   # organisation value that counts as own contracts

FUNNEL = """
with lead_all as (
  select id, subjectid, status, createdat_date, utmcampaign, utmcontent, islowprio,
         split_part(channel__campaign, '::', 1) raw_ch
  from {T_LEAD}
),
base as (
  select id, subjectid, createdat_date d, utmcampaign, utmcontent, islowprio,
         case raw_ch when 'facebook' then 'Meta' when 'fb' then 'Meta' when 'ig' then 'Meta'
                     when 'Search Ads' then 'Google' when 'Search_Ads' then 'Google' when 'google' then 'Google'
                     when 'PerformanceMax' then 'PMax' when 'Display' then 'Display' when 'YouTube' then 'Youtube'
                     when 'outbrain' then 'Outbrain' when 'tiktok' then 'tiktok'
                     when 'affiliation' then null when 'criteo' then null when 'mediago' then null else 'Organic' end ch
  from lead_all
  where createdat_date between %(start)s and %(end)s
    and coalesce(status, '') not in ('MANUAL', 'SALESPARTNER', 'NOMANAGING', 'DUPLICATESALESPARTNER')
),
sc1 as (
  select leadid, min(tsstart) first_sc1
  from {T_CALENDAR_EVENT}
  where netsc1 = 1 and is_passed = 1 and grosssa = 1
  group by 1
),
-- surveys reachable from a base lead: its own requests, plus requests of MANUAL "twin" leads of the same subject
surv as (
  select b.id lead_id, fs.levelofinterest loi, fs.createdat, iff(r.leadid = b.id, 0, 1) twin
  from base b
  join lead_all t on t.subjectid = b.subjectid and (t.id = b.id or t.status = 'MANUAL')
  join {T_REQUEST} r on r.leadid = t.id
  join {T_FINAL_SURVEY} fs on fs.requestid = r.id
  join sc1 s on s.leadid = b.id
  where fs.fromstatusid = 28 and (fs.statusid is null or fs.statusid not in (4, 5))
    and fs.levelofinterest between 1 and 7 and fs.createdat >= s.first_sc1
),
loi as (
  select lead_id, loi from surv
  qualify row_number() over (partition by lead_id order by twin, createdat) = 1
),
ctr as (
  select c.leadid, c.subjectid, c.data_firma_contratto
  from {T_CONTRACTS} c
  where c.netcontractsigned = 1
    and coalesce(c.tipo_prodotto, '') not in ('Batteria', 'Colonnina', 'Caldaia')
    and (c.tipo_prodotto is null or not contains(c.tipo_prodotto, 'C&I'))
    and (c.organizzazione = '{CONTRACT_ORG}' or c.organizzazione is null)
),
-- contract on a MANUAL lead (or an unknown lead) -> the subject's callable lead (CALLABLE first, then lowest id)
callable as (
  select b.subjectid, b.id from base b join lead_all a on a.id = b.id
  qualify row_number() over (partition by b.subjectid order by iff(a.status = 'CALLABLE', 0, 1), b.id) = 1
),
ctr_lead as (
  select coalesce(iff(l.id is null or l.status = 'MANUAL', null, c.leadid), k.id) lead_id, c.data_firma_contratto
  from ctr c
  left join lead_all l on l.id = c.leadid
  left join callable k on k.subjectid = c.subjectid
),
ctr_agg as (
  select lead_id, count(*) n_ctr, min(data_firma_contratto) first_sign from ctr_lead where lead_id is not null group by 1
),
lt as (select id, bc_1, lead_status from {T_LEAD_TIME}),
per_lead as (
  select b.ch, b.utmcampaign, b.utmcontent, b.d,
         iff(b.islowprio = true, 1, 0) low,
         iff(lt.bc_1 is not null, 1, 0) called,
         iff(lt.lead_status = 'REACHED_AND_SC1_SCHEDULED', 1, 0) reached,
         iff(s.leadid is not null, 1, 0) sc1net,
         l.loi, coalesce(a.n_ctr, 0) n_ctr, a.first_sign
  from base b
  left join lt on lt.id = b.id
  left join sc1 s on s.leadid = b.id
  left join loi l on l.lead_id = b.id
  left join ctr_agg a on a.lead_id = b.id
)
"""
for _k, _v in TABLES.items():
    FUNNEL = FUNNEL.replace("{T_" + _k + "}", _v)
FUNNEL = FUNNEL.replace("{CONTRACT_ORG}", CONTRACT_ORG)

NEW_DATA = FUNNEL + """
select ch, utmcampaign, utmcontent, d::string, count(*), sum(low), sum(called), sum(reached), sum(sc1net),
       sum(iff(loi = 1, 1, 0)), sum(iff(loi = 4, 1, 0)), sum(iff(loi = 6, 1, 0)), sum(iff(loi = 7, 1, 0)),
       sum(n_ctr), sum(iff(n_ctr > 0, 1, 0))
from per_lead group by 1, 2, 3, 4
"""

# One row per lead in the sheet, but grouped here: identical leads collapse to a count.
LEAD_CONTRACT = FUNNEL + """
select d::string, first_sign::date::string, ch, utmcontent, n_ctr, iff(n_ctr > 0, 1, 0), count(*)
from per_lead group by 1, 2, 3, 4, 5, 6
"""


def main(start, end, out):
    p = {k: os.getenv(f"SNOWFLAKE_{k.upper()}") for k in ("account", "user", "role", "warehouse", "database")}
    con = snowflake.connector.connect(authenticator="externalbrowser", client_store_temporary_credential=True,
                                      **{k: v for k, v in p.items() if v})
    try:
        cur = con.cursor()
        res = {}
        for key, sql in (("nd", NEW_DATA), ("lcc", LEAD_CONTRACT)):
            print(f"querying {key}...", flush=True)
            cur.execute(sql, {"start": start, "end": end})
            res[key] = [list(r) for r in cur.fetchall()]
            print(f"  {len(res[key]):,} grouped rows", flush=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(res, f)
    finally:
        con.close()


if __name__ == "__main__":
    main(*sys.argv[1:4])
