sqlite3 -header -column reedus_companion/memory/corrections.sqlite \
"SELECT id, signal, verified, substr(subject,1,18) AS subj, substr(attribute,1,22) AS attr,
        substr(user_said,1,34) AS said, fix_ok, substr(fix_reason,1,26) AS why
 FROM corrections ORDER BY ts DESC LIMIT 20;"
