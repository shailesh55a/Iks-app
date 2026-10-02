# IKSphere Mobile — Firebase + Groq setup

## Firebase Web authentication

1. Create or open a Firebase project.
2. Add a Web app from **Project settings → Your apps**.
3. In **Authentication → Sign-in method**, enable:
   - Google
   - Email/Password
   - Anonymous
4. Copy the Web app config into `frontend/.env.local` using `frontend/.env.example` as the template.
5. Add `localhost` to the Firebase Authentication authorized domains when Firebase asks for it.
6. Put the Firebase project ID into `backend/.env` as `FIREBASE_PROJECT_ID` so FastAPI verifies ID tokens.
7. Put teacher email addresses in `TEACHER_EMAILS`, comma-separated. Those accounts are promoted to the Teacher Studio role after Firebase sign-in.

Google sign-in uses Firebase's web authentication SDK. On mobile IKSphere uses the redirect flow; on wider screens it can use the popup flow.

## Groq AI

1. Create a Groq API key.
2. Put it only in `backend/.env`:

```env
GROQ_API_KEY=your_groq_key
GROQ_MODEL=openai/gpt-oss-20b
```

3. Never put the Groq API key into a `VITE_*` variable or browser code.
4. The FastAPI `/api/ai/ask` endpoint is the only application path that calls Groq.
5. If Groq is unavailable or no key is configured, Samvada falls back to the built-in curated IKS knowledge engine.

## Local development files

`frontend/.env.local` controls Firebase Web configuration.

`backend/.env` controls the Groq key, server-side Firebase verification, teacher emails, and CORS.

Both files are ignored by Git in this project. Do not commit secrets.
