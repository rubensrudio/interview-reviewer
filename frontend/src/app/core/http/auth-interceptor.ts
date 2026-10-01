import { HttpErrorResponse, HttpInterceptorFn, HttpXsrfTokenExtractor } from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { catchError, throwError } from 'rxjs';

import { toApiError } from './api-error';

const LOGIN_PATH = '/login';
const ACCEPT_TERMS_PATH = '/accept-terms';

/** Session probe: a 401 here is expected for visitors and is handled by the route guards. */
const SESSION_PROBE_URL = '/api/auth/me';

export const XSRF_HEADER_NAME = 'X-XSRF-TOKEN';

function pathOf(url: string): string {
  return url.split(/[?#]/, 1)[0];
}

function isOn(currentUrl: string, path: string): boolean {
  return pathOf(currentUrl) === path;
}

/**
 * Redirects on session/consent errors (AUTH-17) and always rethrows the original error,
 * so each page can still react to it. Never shows messages itself.
 *
 * Also sends the XSRF header on same-origin HEAD requests: the backend checks it on every
 * non-GET method, but Angular's built-in XSRF interceptor skips GET and HEAD.
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const router = inject(Router);
  const xsrfTokens = inject(HttpXsrfTokenExtractor);

  if (
    req.method === 'HEAD' &&
    !/^https?:\/\//i.test(req.url) &&
    !req.headers.has(XSRF_HEADER_NAME)
  ) {
    const token = xsrfTokens.getToken();
    if (token !== null) {
      req = req.clone({ headers: req.headers.set(XSRF_HEADER_NAME, token) });
    }
  }

  return next(req).pipe(
    catchError((err: unknown) => {
      if (err instanceof HttpErrorResponse) {
        const { code } = toApiError(err);
        const currentUrl = router.url;

        if (
          err.status === 401 &&
          code === 'AUTH_REQUIRED' &&
          pathOf(req.url) !== SESSION_PROBE_URL &&
          !isOn(currentUrl, LOGIN_PATH)
        ) {
          void router.navigateByUrl(`${LOGIN_PATH}?returnUrl=${encodeURIComponent(currentUrl)}`);
        } else if (
          err.status === 403 &&
          code === 'TERMS_REQUIRED' &&
          !isOn(currentUrl, ACCEPT_TERMS_PATH)
        ) {
          void router.navigateByUrl(ACCEPT_TERMS_PATH);
        }
      }
      return throwError(() => err);
    }),
  );
};
