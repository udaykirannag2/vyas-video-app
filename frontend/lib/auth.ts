/**
 * Cognito auth via Amplify v6.
 * Configure once at app startup with NEXT_PUBLIC_COGNITO_* env vars.
 */
import { Amplify } from "aws-amplify";
import {
  signIn as amplifySignIn,
  signUp as amplifySignUp,
  confirmSignUp as amplifyConfirmSignUp,
  signOut as amplifySignOut,
  getCurrentUser,
  fetchAuthSession,
  resendSignUpCode,
} from "aws-amplify/auth";

let configured = false;
export function configureAmplify() {
  if (configured) return;
  const poolId = process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID;
  const clientId = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID;
  if (!poolId || !clientId) {
    console.warn("Cognito env vars missing — auth disabled");
    return;
  }
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: poolId,
        userPoolClientId: clientId,
      },
    },
  });
  configured = true;
}

export async function getIdToken(): Promise<string | null> {
  try {
    const session = await fetchAuthSession();
    return session.tokens?.idToken?.toString() ?? null;
  } catch {
    return null;
  }
}

export async function currentUserEmail(): Promise<string | null> {
  try {
    const user = await getCurrentUser();
    return user.signInDetails?.loginId ?? user.username ?? null;
  } catch {
    return null;
  }
}

export async function signIn(email: string, password: string) {
  return amplifySignIn({ username: email, password });
}

export async function signUp(email: string, password: string) {
  return amplifySignUp({
    username: email,
    password,
    options: { userAttributes: { email } },
  });
}

export async function confirmSignUp(email: string, code: string) {
  return amplifyConfirmSignUp({ username: email, confirmationCode: code });
}

export async function resendCode(email: string) {
  return resendSignUpCode({ username: email });
}

export async function signOut() {
  return amplifySignOut();
}
