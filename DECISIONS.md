# DECISIONS.md

1. **Indexes.** On employees, emp_code is unique. Lookups use it, and it turns a duplicate create into a 409. Department + emp_code serves the department filters, and joined_on serves company-wide headcount. On attendance_logs, emp_code + date is unique, so nobody gets two records for one day. Date + emp_code covers the default list order and monthly ranges. I skipped an index on punch_in for punch-out, because one person only has a few hundred records.

2. **Punch-in race.** Both requests work out the same attendance date and insert straight away, without checking first. The unique index accepts the first write and rejects the second with a duplicate-key error, which I return as 409.

3. **Ties.** I rank with $rank, so tied people share a rank and the next one is skipped. I then keep everyone ranked within the limit, so both tied employees appear, even if that goes over it.

4. **Headcount.** I start from employees who joined by month end and attach their logs. Someone with no logs gets an empty list and is still counted.

5. **100x.** I'd keep a monthly total per employee, updated on a schedule, so reports read small summaries instead of every raw record.
