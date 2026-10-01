import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { firstValueFrom } from 'rxjs';

import { ApiError } from '../http/api-error';
import { AuthApi, Me } from './auth-api';

const ME: Me = {
  id: 'u-1',
  email: 'ada@example.com',
  has_password: true,
  google_linked: false,
  terms_accepted: true,
};

function authRequired(): { status: number; statusText: string } {
  return { status: 401, statusText: 'Unauthorized' };
}

const AUTH_REQUIRED_BODY = {
  error: { code: 'AUTH_REQUIRED', message: 'Authentication required.', details: null },
};

describe('AuthApi', () => {
  let api: AuthApi;
  let httpTesting: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    api = TestBed.inject(AuthApi);
    httpTesting = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpTesting.verify());

  it('starts with no current user', () => {
    expect(api.currentUser()).toBeNull();
  });

  it('login POSTs the credentials to /api/auth/login and stores the user', async () => {
    const result = firstValueFrom(api.login('ada@example.com', 's3cret-pass'));

    const req = httpTesting.expectOne('/api/auth/login');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ email: 'ada@example.com', password: 's3cret-pass' });
    req.flush({ user: ME });

    await expect(result).resolves.toEqual(ME);
    expect(api.currentUser()).toEqual(ME);
  });

  it('login rejects with an ApiError and keeps the user empty', async () => {
    const result = firstValueFrom(api.login('ada@example.com', 'wrong'));

    httpTesting
      .expectOne('/api/auth/login')
      .flush(
        {
          error: {
            code: 'INVALID_CREDENTIALS',
            message: 'Invalid e-mail or password.',
            details: null,
          },
        },
        authRequired(),
      );

    await expect(result).rejects.toEqual({
      code: 'INVALID_CREDENTIALS',
      message: 'Invalid e-mail or password.',
    } satisfies ApiError);
    expect(api.currentUser()).toBeNull();
  });

  it('logout POSTs to /api/auth/logout and clears currentUser', async () => {
    await loginAs(ME);

    const result = firstValueFrom(api.logout(), { defaultValue: undefined });
    const req = httpTesting.expectOne('/api/auth/logout');
    expect(req.request.method).toBe('POST');
    req.flush(null, { status: 204, statusText: 'No Content' });

    await result;
    expect(api.currentUser()).toBeNull();
  });

  it('logout clears currentUser when the session was already gone (401)', async () => {
    await loginAs(ME);

    const result = firstValueFrom(api.logout(), { defaultValue: undefined });
    httpTesting.expectOne('/api/auth/logout').flush(AUTH_REQUIRED_BODY, authRequired());

    await expect(result).rejects.toMatchObject({ code: 'AUTH_REQUIRED' });
    expect(api.currentUser()).toBeNull();
  });

  it('logout keeps currentUser when the server fails for another reason', async () => {
    await loginAs(ME);

    const result = firstValueFrom(api.logout(), { defaultValue: undefined });
    httpTesting.expectOne('/api/auth/logout').flush(
      { error: { code: 'CSRF_FAILED', message: 'CSRF check failed.', details: null } },
      {
        status: 403,
        statusText: 'Forbidden',
      },
    );

    await expect(result).rejects.toMatchObject({ code: 'CSRF_FAILED' });
    expect(api.currentUser()).toEqual(ME);
  });

  it('me GETs /api/auth/me and stores the user', async () => {
    const result = firstValueFrom(api.me());

    const req = httpTesting.expectOne('/api/auth/me');
    expect(req.request.method).toBe('GET');
    req.flush(ME);

    await expect(result).resolves.toEqual(ME);
    expect(api.currentUser()).toEqual(ME);
  });

  it('me clears currentUser on 401', async () => {
    await loginAs(ME);

    const result = firstValueFrom(api.me());
    httpTesting.expectOne('/api/auth/me').flush(AUTH_REQUIRED_BODY, authRequired());

    await expect(result).rejects.toMatchObject({ code: 'AUTH_REQUIRED' });
    expect(api.currentUser()).toBeNull();
  });

  it('register POSTs email, password and accept_terms', async () => {
    const result = firstValueFrom(api.register('ada@example.com', 's3cret-pass'));

    const req = httpTesting.expectOne('/api/auth/register');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({
      email: 'ada@example.com',
      password: 's3cret-pass',
      accept_terms: true,
    });
    req.flush(
      { message: 'Check your e-mail.', email_delivery: 'delayed' },
      { status: 202, statusText: 'Accepted' },
    );

    await expect(result).resolves.toEqual({
      message: 'Check your e-mail.',
      email_delivery: 'delayed',
    });
  });

  it('verifyEmail POSTs the token', async () => {
    const message = 'Your e-mail has been verified. You can now sign in.';
    const result = firstValueFrom(api.verifyEmail('tok-1'));

    const req = httpTesting.expectOne('/api/auth/verify-email');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ token: 'tok-1' });
    req.flush({ message });

    await expect(result).resolves.toEqual({ message });
  });

  it('resendVerification and forgotPassword POST the e-mail', async () => {
    const resend = firstValueFrom(api.resendVerification('ada@example.com'));
    const resendReq = httpTesting.expectOne('/api/auth/resend-verification');
    expect(resendReq.request.method).toBe('POST');
    expect(resendReq.request.body).toEqual({ email: 'ada@example.com' });
    resendReq.flush({ message: 'Sent.' }, { status: 202, statusText: 'Accepted' });
    await expect(resend).resolves.toEqual({ message: 'Sent.' });

    const forgot = firstValueFrom(api.forgotPassword('ada@example.com'));
    const forgotReq = httpTesting.expectOne('/api/auth/forgot-password');
    expect(forgotReq.request.method).toBe('POST');
    expect(forgotReq.request.body).toEqual({ email: 'ada@example.com' });
    forgotReq.flush({ message: 'Maybe.' }, { status: 202, statusText: 'Accepted' });
    await expect(forgot).resolves.toEqual({ message: 'Maybe.' });
  });

  it('resetPassword POSTs token and new_password', async () => {
    const result = firstValueFrom(api.resetPassword('tok-2', 'n3w-password'), {
      defaultValue: undefined,
    });

    const req = httpTesting.expectOne('/api/auth/reset-password');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ token: 'tok-2', new_password: 'n3w-password' });
    req.flush(null, { status: 204, statusText: 'No Content' });

    await expect(result).resolves.toBeUndefined();
  });

  it('linkGoogle POSTs token and password and stores the user', async () => {
    const linked: Me = { ...ME, google_linked: true };
    const result = firstValueFrom(api.linkGoogle('pending-1', 's3cret-pass'));

    const req = httpTesting.expectOne('/api/auth/google/link');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ token: 'pending-1', password: 's3cret-pass' });
    req.flush({ user: linked });

    await expect(result).resolves.toEqual(linked);
    expect(api.currentUser()).toEqual(linked);
  });

  it('acceptTerms POSTs accept=true and marks the current user as accepted', async () => {
    await loginAs({ ...ME, terms_accepted: false });

    const result = firstValueFrom(api.acceptTerms(), { defaultValue: undefined });
    const req = httpTesting.expectOne('/api/account/terms');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ accept: true });
    req.flush(null, { status: 204, statusText: 'No Content' });

    await result;
    expect(api.currentUser()?.terms_accepted).toBe(true);
  });

  it('deleteAccount sends DELETE /api/account and clears currentUser', async () => {
    await loginAs(ME);
    const message = 'Your account and data were deleted. Backup copies expire within 30 days.';

    const result = firstValueFrom(api.deleteAccount());
    const req = httpTesting.expectOne('/api/account');
    expect(req.request.method).toBe('DELETE');
    req.flush({ message });

    await expect(result).resolves.toEqual({ message });
    expect(api.currentUser()).toBeNull();
  });

  it('exposes the Google start URL', () => {
    expect(api.googleStartUrl).toBe('/api/auth/google/start');
  });

  it('never writes to localStorage', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');

    await loginAs(ME);

    expect(setItem).not.toHaveBeenCalled();
    setItem.mockRestore();
  });

  it('rejects with NETWORK_ERROR when the server is unreachable', async () => {
    const result = firstValueFrom(api.me());

    httpTesting.expectOne('/api/auth/me').error(new ProgressEvent('error'));

    await expect(result).rejects.toMatchObject({ code: 'NETWORK_ERROR' });
  });

  async function loginAs(user: Me): Promise<void> {
    const result = firstValueFrom(api.login(user.email, 'whatever'));
    httpTesting.expectOne('/api/auth/login').flush({ user });
    await result;
  }
});
