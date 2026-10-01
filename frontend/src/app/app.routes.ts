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
    path: 'forgot-password',
    title: 'Reset your password',
    loadComponent: () =>
      import('./features/auth/forgot-password-page').then((m) => m.ForgotPasswordPage),
    canActivate: [guestGuard],
  },
  {
    path: 'reset-password',
    title: 'Choose a new password',
    loadComponent: () =>
      import('./features/auth/reset-password-page').then((m) => m.ResetPasswordPage),
    canActivate: [guestGuard],
  },
  {
    path: 'link-google',
    title: 'Link your Google account',
    loadComponent: () => import('./features/auth/link-google-page').then((m) => m.LinkGooglePage),
    canActivate: [guestGuard],
  },
  {
    // Public on purpose (DATA-08): readable with or without a session, so no guard.
    path: 'privacy',
    title: 'Privacy Policy',
    loadComponent: () => import('./features/legal/privacy-page').then((m) => m.PrivacyPage),
  },
  {
    path: 'terms',
    title: 'Terms of Use',
    loadComponent: () => import('./features/legal/terms-page').then((m) => m.TermsPage),
  },
  {
    // Signed-in only: termsGuard sends users here, so it must not run termsGuard itself.
    path: 'accept-terms',
    title: 'Accept the Terms of Use',
    loadComponent: () => import('./features/auth/accept-terms-page').then((m) => m.AcceptTermsPage),
    canActivate: [authGuard],
  },
  {
    path: '',
    loadComponent: () => import('./layout/shell').then((m) => m.Shell),
    canActivate: [authGuard, termsGuard],
    children: [
      {
        path: 'resumes',
        title: 'Resumes',
        loadComponent: () =>
          import('./features/resumes/resume-list-page').then((m) => m.ResumeListPage),
      },
      {
        path: 'resumes/:id',
        title: 'Resume',
        loadComponent: () =>
          import('./features/resumes/resume-detail-page').then((m) => m.ResumeDetailPage),
      },
      {
        path: 'sessions/new',
        title: 'New interview',
        loadComponent: () =>
          import('./features/sessions/new-session-page').then((m) => m.NewSessionPage),
      },
      {
        path: 'sessions/:id',
        title: 'Interview',
        loadComponent: () => import('./features/sessions/session-page').then((m) => m.SessionPage),
      },
      {
        path: 'history',
        title: 'History',
        loadComponent: () => import('./features/history/history-page').then((m) => m.HistoryPage),
      },
      {
        path: 'sessions/:id/report',
        title: 'Interview report',
        loadComponent: () => import('./features/reports/report-page').then((m) => m.ReportPage),
      },
    ],
  },
];
