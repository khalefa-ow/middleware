Write ONE Python file, `setup.py`, in this project folder (it is run there as `python3 setup.py`, so that folder is the current directory). It generates all the data, queries and expected answers for the later FastLanes stages (stage2.md to stage4.md), then checks them, so nothing else is provided to the agent.  Put setup.py directly in this folder (no sub-folder). 
It writes, relative to the current directory (creating data/ if needed): data/column.csv, q1.txt, gold1.txt, data/big.csv, q2.txt and gold2.txt. q1.txt, gold1.txt, q2.txt and gold2.txt go in the folder itself, not in data/. The sizes are the setup_args given at the end of this task: SMALL_ROWS = small_rows, BIG_ROWS = big_rows, and no_queries is the number of queries in q1.txt and in q2.txt. Define COLUMN_CSV = "data/column.csv" and BIG_CSV = "data/big.csv" and use these exact paths for both writing and reading; never write the CSVs at the project root. Create data/ before opening temporary files there. Write the sizes into setup.py as constants: setup.py takes no arguments and reads no config file.

setup_args contract: use the setup_args appended to this task as the sole source of dataset sizes and query count. Embed SMALL_ROWS = int(small_rows), BIG_ROWS = int(big_rows), and NO_QUERIES = int(no_queries) as literal constants in the generated setup.py; these names describe the mapping, not runtime variables or command-line arguments. Do not copy sizes from examples, silently substitute defaults, or read task.json. Validate small_rows >= 2, big_rows >= 2, and no_queries >= 12; reject invalid settings rather than adjusting them. Both query files must have exactly NO_QUERIES lines. Both CSVs must always be generated and validated during setup, with exactly SMALL_ROWS and BIG_ROWS data rows respectively (excluding the header). big_from_stage is runner metadata, not a row count or a setup-generation switch: do not defer big.csv generation based on it. Use the configured constants in disk validation and summary counts too.

1. Data. A CSV has the header `v`, then one value per line. column.csv has SMALL_ROWS values (seed 7), big.csv has BIG_ROWS values (seed 8). A column is strictly increasing integers made by splitmix64, all arithmetic on uint64 (mask with 2^64-1 in Python):
     state = seed
     next(): state += 0x9E3779B97F4A7C15; z = state; z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9; z = (z ^ (z >> 27)) * 0x94D049BB133111EB; return z ^ (z >> 31)
     key = 0; for each row: key += 1 + next() % 20; write key.
   Keep the columns in array('q') rather than lists of Python ints, and write the CSV in large chunks to a temporary file that is then renamed, so a half-written file is never left behind (do the same for the query, gold and stamp files). The 20M-row set must take well under a minute and a few hundred MB of memory. Store prefix sums in array('q') too. Process one dataset at a time and release generation values and prefix arrays before re-reading that dataset for validation; release validation arrays before proceeding to the next dataset. Never create set(values) or a list of all values: test membership with bisect_left and a bounds/equality check on the sorted array.

2. Queries. Predicates test the column value `v`, not the row index. Every line is one complete query with exactly these spaces and a trailing semicolon:
     SUM(v);
     SUM(v) WHERE v <op> <integer>;           op is one of = < <= > >=
     SUM(v) WHERE v BETWEEN <a> AND <b>;      inclusive on both ends
   Every line must full-match this regular expression:
     ^SUM\(v\)(?: WHERE v (=|<|<=|>|>=) (-?[0-9]+)| WHERE v BETWEEN (-?[0-9]+) AND (-?[0-9]+))?;$
   No WHERE i, !=, bare predicates, omitted v, lower-case keywords or extra spaces. For example, `SUM(v) WHERE v BETWEEN 501 AND 96488;` is valid; `SUM(v) WHERE BETWEEN 501 AND 96488;` is invalid. BETWEEN and AND belong to one predicate and must not split the query.
   q1.txt and q2.txt each contain exactly no_queries queries. Use fixed-seed random.Random. Each file must include a whole-column sum, every comparison operator, and BETWEEN cases covering a nonempty range, equal endpoints matching an existing value, reversed endpoints (5 AND 4), endpoints in gaps, and a range covering the entire column. Require no_queries >= 12. Fill remaining slots deterministically with point lookups and ranges of varied sizes. Choose constants from the column values and gaps, not row positions. Include the exact regression query `SUM(v) WHERE v BETWEEN 501 AND 96488;` in each file.

3. Gold. gold1.txt and gold2.txt contain one exact integer answer per query as `Q<n>: <sum>`, n starting at 1. Parse each complete line using the regex above; reject any mismatch. For sorted values, let L(x) = bisect_left(values, x), U(x) = bisect_right(values, x). Convert predicates into half-open row ranges: = x uses [L(x), U(x)); < x uses [0, L(x)); <= x uses [0, U(x)); > x uses [U(x), N); >= x uses [L(x), N); BETWEEN a AND b uses [L(a), U(b)), empty when a > b. A whole-column sum uses [0, N). Answer using prefix[hi] - prefix[lo], with exact integer sums and 0 for empty selections. Never interpret constants as row indices.

   Dispatch the four regex captures explicitly in every answer routine (including independent verification):
     op, literal, lower, upper = match.groups()
     if op is None and lower is None:       # SUM(v); all captures are None
         return prefix[-1]                 # brute-force verifier returns sum(values)
     elif op is not None:                  # comparison; only literal is converted
         x = int(literal)
         # dispatch =, <, <=, >, >= using the ranges above
     else:                                 # BETWEEN; only lower/upper are converted
         a, b = int(lower), int(upper)
         if a > b:
             return 0
         # return prefix[U(b)] - prefix[L(a)]
   Never call int() on an unmatched capture, and never infer BETWEEN just from op being None: that is also true for SUM(v);.
   Before generating either large dataset, run silent small-fixture assertions through the actual gold-answer routine and the brute-force verifier. With values = array('q', [10, 20, 40]) and prefix = array('q', [0, 10, 30, 70]), require these query/result pairs:
     SUM(v); -> 70
     SUM(v) WHERE v = 20; -> 20
     SUM(v) WHERE v < 20; -> 10
     SUM(v) WHERE v <= 20; -> 30
     SUM(v) WHERE v > 20; -> 40
     SUM(v) WHERE v >= 20; -> 60
     SUM(v) WHERE v BETWEEN 20 AND 40; -> 60
     SUM(v) WHERE v BETWEEN 40 AND 20; -> 0
     SUM(v) WHERE v BETWEEN 20 AND 20; -> 20
     SUM(v) WHERE v BETWEEN 11 AND 39; -> 20
     SUM(v) WHERE v BETWEEN 501 AND 96488; -> 0
   These assertions must not write files or print extra stdout.

4. Check. After generating, setup.py checks what is on disk, without trusting anything it computed while generating: it reads the files back and
   - reads data/column.csv and data/big.csv (header `v`), checks that each column is strictly increasing, and computes the row count, first and last value and the exact int64 sum;
   - checks that every line of q1.txt and q2.txt matches the regular expression above, and that q1.txt and q2.txt each have exactly no_queries lines;
   - checks that every gold line has the form `Q<n>: <integer>` with n counting from 1, and that gold1.txt and gold2.txt have as many lines as q1.txt and q2.txt;
   - recomputes every answer with a second method and checks it equals the gold value: q1 by brute force over column.csv, testing each value directly against the parsed operator and constants; q2 from a prefix-sum array built afresh from the values read back from big.csv (not the one used while generating).
   - checks the required query-kind and edge-case coverage, including the exact BETWEEN regression query; explicitly verifies that the regression parses as BETWEEN with bounds 501 and 96488.
   - validates gap endpoints by checking that BOTH endpoints lie strictly between adjacent existing values, using binary searches. This does not require the interval to be empty: on the fixture above, BETWEEN 11 AND 39 has two gap endpoints and sums to 20. For equal existing endpoints use binary-search membership, not set(values).
   - checks the CSVs exist at data/column.csv and data/big.csv, and requires SUM(v); to return the full sum in both gold files.
   On success it prints exactly three summary lines and exits 0:
     column: rows=<N> first=<v> last=<v> sum=<s>
     big: rows=<N> first=<v> last=<v> sum=<s>
     queries: q1=<count> q2=<count>
   Use the exact labels, equals signs and spacing shown above, including first and last values; do not substitute prose such as "300000 rows" or "40 per file".
   On any failed check it prints what failed on stderr and exits with a non-zero code.
