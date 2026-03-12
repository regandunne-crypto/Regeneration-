Render deployment steps

1. Create a new GitHub repository and upload these files.
2. In Render, choose New + > Web Service.
3. Connect the GitHub repository.
4. Render should detect render.yaml automatically. If not, use:
   Build Command: pip install -r requirements.txt
   Start Command: uvicorn server:app --host 0.0.0.0 --port $PORT
5. After deploy, open the Render URL.
6. Students use the main URL. Lecturer uses the same URL with #host at the end.

Important: the lecturer passcode is still stored in app.js, so this is convenience-level protection, not secure authentication.
