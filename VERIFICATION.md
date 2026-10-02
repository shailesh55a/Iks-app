# IKSphere Mobile 5.0 — Verification Report

## Runtime architecture

- React + Vite mobile-first PWA
- Node.js + Express production server
- Python + FastAPI API
- SQLite persistence
- Firebase Web Authentication integration (Google, Email/Password, Anonymous)
- Groq-only AI integration with offline curated fallback
- No Kotlin / Android runtime
- No college syllabus or unrelated Data Visualization curriculum in the shipped runtime

## Automated checks

- Backend regression suite: **11 passed**
- Python backend compile check: **PASS**
- Node production server syntax: **PASS**
- Frontend-to-API method contract scan: **PASS** (31 referenced API methods, all implemented)
- Frontend/package/manifest JSON parse: **PASS**
- Runtime legacy-content scan: **PASS** (no Data Visualization, Gemini, or `/syllabus` runtime references)

## Live FastAPI smoke checks

Verified on a clean seeded database:

- `/api/health`
- `/api/config`
- Firebase/local guest authentication paths
- `/api/home/{user_id}`
- `/api/users/{user_id}/dashboard`
- `/api/courses`
- `/api/paths`
- `/api/quizzes`
- `/api/scholars`
- `/api/bookmarks/{user_id}`
- `/api/notes/{user_id}`
- `/api/achievements/{user_id}`
- `/api/analytics/{user_id}`
- `/api/sanskrit-guide`
- `/api/lessons/{lesson_id}`
- `/api/search`
- `/api/ai/history/{user_id}`

## Product flows covered by regression tests

- Guest sign-in
- Firebase-style local token registration and user/bearer binding
- Course loading with fresh lessons marked incomplete
- Lesson progress / XP
- Lesson-specific bookmarks and notes
- Quiz payload safety and scoring
- Teacher course / quiz / learning-path creation
- Non-teacher write protection
- Course-completion certificate PDF generation
- Sanskrit guide
- Offline AI response path
- Search excluding retired syllabus content
- IKS source metadata present on seeded courses

## Dependency-install limitation

This build environment could not resolve `registry.npmjs.org` (`EAI_AGAIN`), so a fresh `npm install` and Vite production build could not be executed here. The repository contains the complete package configuration and the run/build commands are documented in `README.md`. This is an environment/network limitation, not a reported application runtime error.
