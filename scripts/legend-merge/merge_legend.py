"""
merge_legend.py  --  Name the untracked IDs in a GTM cohort export.

the ads manager's GTM file (New_Data shape) uses raw ad IDs in the "UTM Content"
column for creatives that aren't named yet -> these show up as "untracked"
on the Creative Performance dashboard. This script replaces those raw IDs
with the creative name from the data analyst's legend (creative_legend.xlsx).

Only raw numeric IDs are renamed; already-named UTM tags and every other
column are left byte-for-byte identical. Schema (14 cols) is preserved.

USAGE
  python merge_legend.py                                  # uses the defaults below
  python merge_legend.py "GTM_new.csv"                    # custom GTM file
  python merge_legend.py "GTM_new.csv" "creative_legend.xlsx"

OUTPUTS  (written next to the GTM file)
  <gtm>_NAMED.csv            -> the file to use for the report
  <gtm>_still_untracked.csv  -> IDs still missing from the legend (add these next)
"""
import csv, re, sys, os
from collections import defaultdict
import openpyxl

DL = os.environ.get("DOWNLOADS_DIR", os.path.expanduser("~/Downloads"))
DEFAULT_GTM    = os.path.join(DL, "owner GTM_2026-06-29-1536.csv")
DEFAULT_LEGEND = os.path.join(DL, "creative_legend.xlsx")

ID_RE = re.compile(r"\d{6,}=?$")  # raw numeric ad id (untracked-looking)


def load_legend(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    leg = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        key = str(row[0]).strip()
        name = row[1]
        if name is not None and str(name).strip():
            leg[key] = str(name).strip()
    return leg


def resolve(utm, leg):
    for k in (str(utm).strip(), str(utm).strip().rstrip("=").strip()):
        if k in leg:
            return leg[k]
    return None


def main():
    gtm    = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_GTM
    legend = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_LEGEND
    base, _ = os.path.splitext(gtm)
    out_named = base + "_NAMED.csv"
    out_report = base.rsplit("-", 1)[0] + "_still_untracked.csv"

    leg = load_legend(legend)

    with open(gtm, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)

    uc = header.index("UTM Content")
    lc = header.index("Lead Cohort")

    changed = 0
    named_ids = set()
    still = defaultdict(lambda: [0, 0])  # raw id -> [rows, lead_cohort_sum]

    out = []
    for r in rows:
        utm = r[uc]
        is_id = bool(ID_RE.match(utm.strip()))
        name = resolve(utm, leg) if is_id else None
        if name is not None and name != utm:
            r = list(r); r[uc] = name
            changed += 1
            named_ids.add(utm.strip().rstrip("="))
        elif is_id:
            try:
                leads = int(float(r[lc])) if r[lc] else 0
            except ValueError:
                leads = 0
            still[utm.strip()][0] += 1
            still[utm.strip()][1] += leads
        out.append(r)

    with open(out_named, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(header); w.writerows(out)

    with open(out_report, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Raw UTM / Ad ID", "Rows", "Lead Cohort (sum)", "Note"])
        for rid, (cnt, leads) in sorted(still.items(), key=lambda x: -x[1][1]):
            w.writerow([rid, cnt, leads, "not in legend - add a name"])

    print("Rows total              :", len(rows))
    print("Rows renamed via legend :", changed)
    print("Distinct IDs named      :", len(named_ids))
    print("Distinct IDs still untracked:", len(still),
          "(", sum(v[0] for v in still.values()), "rows,",
          sum(v[1] for v in still.values()), "leads )")
    print("\nWrote:", out_named)
    print("Wrote:", out_report)


if __name__ == "__main__":
    main()
