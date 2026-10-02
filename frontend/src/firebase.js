import { initializeApp } from 'firebase/app';
import {
  getAuth,
  GoogleAuthProvider,
  browserLocalPersistence,
  createUserWithEmailAndPassword,
  initializeAuth,
  onAuthStateChanged,
  setPersistence,
  signInAnonymously,
  signInWithEmailAndPassword,
  signInWithPopup,
  signInWithRedirect,
  signOut,
  updateProfile,
  browserPopupRedirectResolver,
} from 'firebase/auth';

const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY || '',
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN || '',
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID || '',
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET || '',
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID || '',
  appId: import.meta.env.VITE_FIREBASE_APP_ID || '',
};

export const firebaseConfigured = Object.values(firebaseConfig).every(Boolean);

let auth = null;
let firebaseApp = null;
if (firebaseConfigured) {
  firebaseApp = initializeApp(firebaseConfig);
  try {
    auth = initializeAuth(firebaseApp, {
      persistence: [browserLocalPersistence],
      popupRedirectResolver: browserPopupRedirectResolver,
    });
  } catch {
    auth = getAuth(firebaseApp);
  }
  setPersistence(auth, browserLocalPersistence).catch(() => {});
}

export { auth };

export function observeFirebaseAuth(callback) {
  if (!auth) return () => {};
  return onAuthStateChanged(auth, callback);
}

function isMobileViewport() {
  return window.matchMedia?.('(max-width: 820px)')?.matches || window.innerWidth <= 820;
}

export async function loginWithGoogle() {
  if (!auth) throw new Error('Firebase is not configured. Add VITE_FIREBASE_* values to frontend/.env.local.');
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: 'select_account' });
  if (isMobileViewport()) {
    await signInWithRedirect(auth, provider);
    return null;
  }
  return signInWithPopup(auth, provider);
}

export async function loginWithEmail(email, password) {
  if (!auth) throw new Error('Firebase is not configured.');
  return signInWithEmailAndPassword(auth, email.trim(), password);
}

export async function createEmailAccount(name, email, password) {
  if (!auth) throw new Error('Firebase is not configured.');
  const cred = await createUserWithEmailAndPassword(auth, email.trim(), password);
  if (name.trim()) await updateProfile(cred.user, { displayName: name.trim() });
  return cred;
}

export async function loginAsGuest() {
  if (!auth) throw new Error('Firebase is not configured. Use local guest mode instead.');
  return signInAnonymously(auth);
}

export async function firebaseLogout() {
  if (auth) await signOut(auth);
}
