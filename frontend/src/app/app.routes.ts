import { Routes } from '@angular/router';

import { authGuard, termsGuard } from './core/auth/auth-guards';

export const routes: Routes = [
  {
    path: '',
    loadComponent: () => import('./layout/shell').then((m) => m.Shell),
    canActivate: [authGuard, termsGuard],
    children: [],
  },
];
