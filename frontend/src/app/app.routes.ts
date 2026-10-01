import { Routes } from '@angular/router';

import { authGuard, guestGuard, termsGuard } from './core/auth/auth-guards';

export const routes: Routes = [
  {
    path: 'register',
    title: 'Create your account',
    loadComponent: () => import('./features/auth/register-page').then((m) => m.RegisterPage),
    canActivate: [guestGuard],
  },
  {
    path: '',
    loadComponent: () => import('./layout/shell').then((m) => m.Shell),
    canActivate: [authGuard, termsGuard],
    children: [],
  },
];
