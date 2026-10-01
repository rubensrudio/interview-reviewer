import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { EnvironmentInjector, runInInjectionContext } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import {
  ActivatedRouteSnapshot,
  CanActivateFn,
  GuardResult,
  MaybeAsync,
  provideRouter,
  Router,
  RouterStateSnapshot,
  UrlTree,
} from '@angular/router';
import { firstValueFrom, isObservable } from 'rxjs';

import { AuthApi, Me } from './auth-api';
import { authGuard, guestGuard, termsGuard } from './auth-guards';

const ME: Me = {
  id: 'u-1',
  email: 'ada@example.com',
  has_password: true,
  google_linked: false,
  terms_accepted: true,
};

const AUTH_REQUIRED_BODY = {
  error: { code: 'AUTH_REQUIRED', message: 'Authentication required.', details: null },
};

describe('auth guards', () => {
  let httpTesting: HttpTestingController;
  let router: Router;
  let api: AuthApi;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    });
    httpTesting = TestBed.inject(HttpTestingController);
    router = TestBed.inject(Router);
    api = TestBed.inject(AuthApi);
  });

  afterEach(() => httpTesting.verify());

  function run(guard: CanActivateFn, url = '/resumes'): Promise<GuardResult> {
    const result: MaybeAsync<GuardResult> = runInInjectionContext(
      TestBed.inject(EnvironmentInjector),
      () => guard({} as ActivatedRouteSnapshot, { url } as RouterStateSnapshot),
    );
    if (isObservable(result)) {
      return firstValueFrom(result);
    }
    return Promise.resolve(result);
  }

  function serialized(result: GuardResult): string {
    expect(result).toBeInstanceOf(UrlTree);
    return router.serializeUrl(result as UrlTree);
  }

  async function signIn(user: Me): Promise<void> {
    const login = firstValueFrom(api.login(user.email, 'whatever'));
    httpTesting.expectOne('/api/auth/login').flush({ user });
    await login;
  }

  describe('authGuard', () => {
    it('returns a UrlTree to /login with the returnUrl when me() answers 401', async () => {
      const result = run(authGuard, '/sessions/42');
      httpTesting.expectOne('/api/auth/me').flush(AUTH_REQUIRED_BODY, {
        status: 401,
        statusText: 'Unauthorized',
      });

      const tree = await result;
      expect(serialized(tree)).toBe('/login?returnUrl=%2Fsessions%2F42');
      expect((tree as UrlTree).root.children['primary'].segments.map((s) => s.path)).toEqual([
        'login',
      ]);
    });

    it('allows navigation when me() answers 200', async () => {
      const result = run(authGuard);
      httpTesting.expectOne('/api/auth/me').flush(ME);

      await expect(result).resolves.toBe(true);
      expect(api.currentUser()).toEqual(ME);
    });

    it('sends the visitor to /login when the server cannot be reached', async () => {
      const result = run(authGuard);
      httpTesting.expectOne('/api/auth/me').error(new ProgressEvent('error'));

      expect(serialized(await result)).toBe('/login?returnUrl=%2Fresumes');
    });

    it('reuses the known user without calling me()', async () => {
      await signIn(ME);

      await expect(run(authGuard)).resolves.toBe(true);
      httpTesting.expectNone('/api/auth/me');
    });
  });

  describe('termsGuard', () => {
    it('redirects a user without acceptance to /accept-terms', async () => {
      await signIn({ ...ME, terms_accepted: false });

      expect(serialized(await run(termsGuard))).toBe('/accept-terms');
    });

    it('allows a user who accepted the terms', async () => {
      await signIn(ME);

      await expect(run(termsGuard)).resolves.toBe(true);
    });

    it('asks me() when the user is unknown', async () => {
      const result = run(termsGuard);
      httpTesting.expectOne('/api/auth/me').flush({ ...ME, terms_accepted: false });

      expect(serialized(await result)).toBe('/accept-terms');
    });

    it('sends a visitor to /login', async () => {
      const result = run(termsGuard, '/history');
      httpTesting.expectOne('/api/auth/me').flush(AUTH_REQUIRED_BODY, {
        status: 401,
        statusText: 'Unauthorized',
      });

      expect(serialized(await result)).toBe('/login?returnUrl=%2Fhistory');
    });

    it('shares a single me() call with authGuard on the same navigation', async () => {
      const auth = run(authGuard);
      const terms = run(termsGuard);
      httpTesting.expectOne('/api/auth/me').flush(ME);

      await expect(auth).resolves.toBe(true);
      await expect(terms).resolves.toBe(true);
    });
  });

  describe('guestGuard', () => {
    it('lets a visitor in when me() answers 401', async () => {
      const result = run(guestGuard, '/login');
      httpTesting.expectOne('/api/auth/me').flush(AUTH_REQUIRED_BODY, {
        status: 401,
        statusText: 'Unauthorized',
      });

      await expect(result).resolves.toBe(true);
    });

    it('lets a visitor in when the server cannot be reached', async () => {
      const result = run(guestGuard, '/login');
      httpTesting.expectOne('/api/auth/me').error(new ProgressEvent('error'));

      await expect(result).resolves.toBe(true);
    });

    it('sends an authenticated user to /resumes', async () => {
      const result = run(guestGuard, '/login');
      httpTesting.expectOne('/api/auth/me').flush(ME);

      expect(serialized(await result)).toBe('/resumes');
    });

    it('does not navigate by itself on 401', async () => {
      const navigate = vi.spyOn(router, 'navigateByUrl');
      const result = run(guestGuard, '/login');
      httpTesting.expectOne('/api/auth/me').flush(AUTH_REQUIRED_BODY, {
        status: 401,
        statusText: 'Unauthorized',
      });

      await result;
      expect(navigate).not.toHaveBeenCalled();
    });
  });
});
