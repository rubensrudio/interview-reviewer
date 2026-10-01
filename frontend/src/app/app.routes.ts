import { Routes } from '@angular/router';

import { authGuard, guestGuard, termsGuard } from './core/auth/auth-guards';

export const routes: Routes = [
  {
    path: 'login',
    title: 'Sign in',
    loadComponent: () => import('./features/auth/login-page').then((m) => m.LoginPage),
    canActivate: [guestGuard],
  },
  {
    path: 'register',
    title: 'Create your account',
    loadComponent: () => import('./features/auth/register-page').then((m) => m.RegisterPage),
    canActivate: [guestGuard],
  },
  {
    path: 'verify-email',
    title: 'Verify your e-mail',
    loadComponent: () => import('./features/auth/verify-email-page').then((m) => m.VerifyEmailPage),
    canActivate: [guestGuard],
  },
  {
    path: '',
    loadComponent: () => import('./layout/shell').then((m) => m.Shell),
    canActivate: [authGuard, termsGuard],
    children: [],
  },
];
