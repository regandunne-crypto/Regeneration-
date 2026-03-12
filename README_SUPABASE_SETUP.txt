SUPABASE UPDATE REQUIRED
========================

This version adds:
- lecturer accounts (sign up / sign in)
- draft saves while building tests
- editing existing tests you created

IMPORTANT:
Run the UPDATED file `supabase_schema.sql` again in the Supabase SQL Editor.
It is written to be idempotent, so it can be re-run safely.

Then make sure Render still has these environment variables:
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY

After uploading this version to GitHub, Render should redeploy automatically.
