SUPABASE SETUP FOR SAVED TESTS
==============================

1. Create a Supabase project.
2. Open the SQL Editor in Supabase and run the contents of supabase_schema.sql.
3. In Supabase, copy:
   - Project URL  -> use as SUPABASE_URL
   - service_role key -> use as SUPABASE_SERVICE_ROLE_KEY
4. In Render, open the engineering-quiz service.
5. Go to Environment and add:
   - SUPABASE_URL
   - SUPABASE_SERVICE_ROLE_KEY
6. Save the environment variables and redeploy the service.
7. Open the lecturer panel again. The test library badge should say “Supabase storage active”.

IMPORTANT
---------
Only put the service_role key in Render (server-side). Do not put it in app.js or any browser code.

WHAT CHANGED
------------
- Host flow is now: Subject -> Test Library -> Use Existing Test / Create New Test.
- Saved tests appear per subject.
- If Supabase env vars are not set, the app falls back to temporary in-memory storage.
