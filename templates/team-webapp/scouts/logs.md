## Scout assignment — new errors in the application logs

The prepared input groups the error lines of the app's log files since the
last run (fetched by code; similar lines are merged by a fingerprint with
numbers / ids blanked out, count + first and last example per group).

For each group that is new or growing and points at a bug in this repo:
read the code path, decide whether it is a real defect (not a client error,
not an expected timeout), and propose a fix task — title, 2-5 lines with the
fingerprint, count, example line, suspected cause (file:line) and how to
verify. Ignore noise and infrastructure problems outside the app (say so in
one line). Write every proposal **in German**.
