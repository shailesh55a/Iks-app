const API_BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '');
let session = { token: '', userId: '' };

export function setApiSession(next) { session = { token: next?.token || '', userId: next?.userId || '' }; }
export function clearApiSession() { session = { token: '', userId: '' }; }

const OFFLINE_CACHE_PREFIX = 'iksphere_cache:';
const OFFLINE_CACHEABLE = /^(\/courses(?:\/[^?]+)?|\/home\/[^?]+|\/paths|\/scholars|\/flashcards|\/quizzes(?:\/[^?]+)?|\/lessons\/[^?]+|\/sanskrit-guide)/;

function cacheKey(path) { return `${OFFLINE_CACHE_PREFIX}${path}`; }
function canCache(path, options) { return (options.method || 'GET').toUpperCase() === 'GET' && OFFLINE_CACHEABLE.test(path); }

async function request(path, options = {}) {
  const headers = { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) };
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  } catch (error) {
    if (canCache(path, options)) {
      try {
        const cached = localStorage.getItem(cacheKey(path));
        if (cached) return JSON.parse(cached);
      } catch {}
    }
    throw new Error('Network unavailable. Please reconnect and try again.');
  }
  const contentType = res.headers.get('content-type') || '';
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try { const data = contentType.includes('json') ? await res.json() : { detail: await res.text() }; if (Array.isArray(data.detail)) message = data.detail.map(x => x.msg || 'Invalid input').join('; '); else message = data.detail || data.message || message; } catch {}
    throw new Error(message);
  }
  if (contentType.includes('application/pdf')) return res.blob();
  const payload = res.status === 204 ? null : await res.json();
  if (payload !== null && canCache(path, options)) { try { localStorage.setItem(cacheKey(path), JSON.stringify(payload)); } catch {} }
  return payload;
}

export const api = {
  health: () => request('/health'),
  config: () => request('/config'),
  authGuest: name => request('/auth/guest', { method:'POST', body:JSON.stringify({name}) }),
  authFirebase: body => request('/auth/firebase', { method:'POST', body:JSON.stringify(body) }),
  authLocal: body => request('/auth/login', { method:'POST', body:JSON.stringify(body) }),
  dashboard: id => request(`/users/${id}/dashboard?user_id=${encodeURIComponent(id)}`),
  home: id => request(`/home/${id}?user_id=${encodeURIComponent(id)}`),
  courses: id => request(`/courses?user_id=${encodeURIComponent(id)}`),
  course: (id, userId) => request(`/courses/${id}?user_id=${encodeURIComponent(userId)}`),
  search: (q,id) => request(`/search?q=${encodeURIComponent(q)}&user_id=${encodeURIComponent(id)}`),
  paths: id => request(`/paths?user_id=${encodeURIComponent(id)}`),
  scholars: () => request('/scholars'),
  lesson: (id,userId) => request(`/lessons/${id}?user_id=${encodeURIComponent(userId)}`),
  progress: (id,completed,userId) => request(`/lessons/${id}/progress?user_id=${encodeURIComponent(userId)}`, {method:'POST',body:JSON.stringify({completed})}),
  bookmarks: id => request(`/bookmarks/${encodeURIComponent(id)}`),
  toggleBookmark: (lessonId,userId) => request(`/bookmarks?user_id=${encodeURIComponent(userId)}`, {method:'POST',body:JSON.stringify({lesson_id:lessonId})}),
  notes: id => request(`/notes/${encodeURIComponent(id)}`),
  createNote: (body,id) => request(`/notes?user_id=${encodeURIComponent(id)}`, {method:'POST',body:JSON.stringify(body)}),
  deleteNote: (noteId,id) => request(`/notes/${encodeURIComponent(noteId)}?user_id=${encodeURIComponent(id)}`, {method:'DELETE'}),
  quizzes: id => request(`/quizzes?user_id=${encodeURIComponent(id)}`),
  quiz: (id,userId) => request(`/quizzes/${id}?user_id=${encodeURIComponent(userId)}`),
  attempt: (id,answers,userId) => request(`/quizzes/${id}/attempt?user_id=${encodeURIComponent(userId)}`, {method:'POST',body:JSON.stringify({answers})}),
  createQuiz: (body,id) => request(`/quizzes?user_id=${encodeURIComponent(id)}`, {method:'POST',body:JSON.stringify(body)}),
  flashcards: courseId => request(courseId ? `/flashcards?course_id=${encodeURIComponent(courseId)}` : '/flashcards'),
  flashcardReview: id => request(`/flashcards/review?user_id=${encodeURIComponent(id)}`),
  reviewFlashcard: (cardId,rating,userId) => request(`/flashcards/${encodeURIComponent(cardId)}/review?user_id=${encodeURIComponent(userId)}`, {method:'POST',body:JSON.stringify({rating})}),
  classrooms: id => request(`/classrooms?user_id=${encodeURIComponent(id)}`),
  createClassroom: (body,id) => request(`/classrooms?user_id=${encodeURIComponent(id)}`, {method:'POST',body:JSON.stringify(body)}),
  joinClassroom: (code,id) => request(`/classrooms/join?user_id=${encodeURIComponent(id)}`, {method:'POST',body:JSON.stringify({join_code:code})}),
  classroomAssignments: (classroomId,id) => request(`/classrooms/${encodeURIComponent(classroomId)}/assignments?user_id=${encodeURIComponent(id)}`),
  assignClassroomContent: (classroomId,body,id) => request(`/classrooms/${encodeURIComponent(classroomId)}/assignments?user_id=${encodeURIComponent(id)}`, {method:'POST',body:JSON.stringify(body)}),
  analytics: id => request(`/analytics/${encodeURIComponent(id)}`),
  achievements: id => request(`/achievements/${encodeURIComponent(id)}`),
  createCourse: (body,id) => request(`/courses?user_id=${encodeURIComponent(id)}`, {method:'POST',body:JSON.stringify(body)}),
  updateCourse: (id,body,userId) => request(`/courses/${id}?user_id=${encodeURIComponent(userId)}`, {method:'PUT',body:JSON.stringify(body)}),
  createPath: (body,id) => request(`/paths?user_id=${encodeURIComponent(id)}`, {method:'POST',body:JSON.stringify(body)}),
  certificateBlob: (id,userId) => request(`/certificates/${id}/pdf?user_id=${encodeURIComponent(userId)}`),
  aiHistory: id => request(`/ai/history/${encodeURIComponent(id)}`),
  clearAiHistory: id => request(`/ai/history/${encodeURIComponent(id)}`, {method:'DELETE'}),
  ask: (message,context,userId,history) => request(`/ai/ask?user_id=${encodeURIComponent(userId)}`, {method:'POST',body:JSON.stringify({message,context,history})}),
  sanskritGuide: () => request('/sanskrit-guide'),
};
