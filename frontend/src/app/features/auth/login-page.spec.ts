import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Observable, Subject, of, throwError } from 'rxjs';

import { AuthApi, Me, MessageResponse } from '../../core/auth/auth-api';
import { ApiError } from '../../core/http/api-error';
import { LoginPage, safeReturnUrl } from './login-page';

@Component({ template: '' })
class BlankPage {}

const INVALID = 'Invalid e-mail or password.';
const TOO_MANY = 'Too many attempts. Please try again later.';
const NOT_VERIFIED = 'Please verify your e-mail before signing in. Resend verification e-mail?';
const GOOGLE_FAILED = 'Google sign-in failed or was cancelled. Please try again.';

const ME: Me = {
  id: 'u1',
  email: 'ana@example.com',
  has_password: true,
  google_linked: false,
  terms_accepted: true,
};

class AuthApiStub {
  readonly googleStartUrl = '/api/auth/google/start';
  login = vi.fn<(email: string, password: string) => Observable<Me>>(() => of(ME));
  resendVerification = vi.fn<(email: string) => Observable<MessageResponse>>(() =>
    of({ message: 'If the account needs verification, we sent a new e-mail.' }),
  );
}

describe('LoginPage', () => {
  let fixture: ComponentFixture<LoginPage>;
  let root: HTMLElement;
  let api: AuthApiStub;
  let router: Router;

  const input = (id: string): HTMLInputElement => {
    const el = root.querySelector<HTMLInputElement>(`#${id}`);
    if (!el) {
      throw new Error(`Input #${id} not found`);
    }
    return el;
  };
  const findButton = (label: string): HTMLButtonElement | undefined =>
    Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => b.textContent?.trim() === label,
    );
  const button = (label: string): HTMLButtonElement => {
    const found = findButton(label);
    if (!found) {
      throw new Error(`Button "${label}" not found`);
    }
    return found;
  };
  const alerts = (): string[] =>
    Array.from(root.querySelectorAll('[role="alert"]')).map((el) => el.textContent?.trim() ?? '');
  const type = (id: string, value: string): void => {
    const el = input(id);
    el.value = value;
    el.dispatchEvent(new Event('input'));
  };
  const render = async (): Promise<void> => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };
  const fillValid = async (): Promise<void> => {
    type('login-email', 'ana@example.com');
    type('login-password', 'correct horse battery');
    await render();
  };
  const submit = async (): Promise<void> => {
    button('Sign in').click();
    await render();
  };
  const failWith = (err: ApiError): void => {
    api.login.mockReturnValue(throwError(() => err));
  };

  const setup = async (url = '/login'): Promise<void> => {
    api = new AuthApiStub();
    await TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'login', component: LoginPage },
          { path: '**', component: BlankPage },
        ]),
        { provide: AuthApi, useValue: api },
      ],
    }).compileComponents();
    router = TestBed.inject(Router);
    await router.navigateByUrl(url);
    fixture = TestBed.createComponent(LoginPage);
    root = fixture.nativeElement as HTMLElement;
    await render();
  };

  describe('form', () => {
    beforeEach(() => setup());

    it('labels every field with sign-in autocomplete hints', () => {
      for (const id of ['login-email', 'login-password']) {
        const label = root.querySelector(`label[for="${id}"]`);
        expect(label?.textContent?.trim()).toBeTruthy();
        expect(input(id).getAttribute('maxlength')).toBe('1024');
      }
      expect(input('login-email').type).toBe('email');
      expect(input('login-password').type).toBe('password');
      expect(input('login-email').getAttribute('autocomplete')).toBe('email');
      expect(input('login-password').getAttribute('autocomplete')).toBe('current-password');
    });

    it('flags empty fields without calling the API', async () => {
      await submit();

      expect(api.login).not.toHaveBeenCalled();
      expect(input('login-email').getAttribute('aria-invalid')).toBe('true');
      expect(input('login-password').getAttribute('aria-invalid')).toBe('true');
      const emailError = root.querySelector(
        `#${input('login-email').getAttribute('aria-describedby')}`,
      );
      expect(emailError?.textContent?.trim()).toBe('Enter your e-mail.');
      expect(document.activeElement).toBe(input('login-email'));
    });

    it('sends the e-mail and password as typed', async () => {
      await fillValid();
      await submit();

      expect(api.login).toHaveBeenCalledWith('ana@example.com', 'correct horse battery');
    });

    it('marks the button busy and ignores double submits while waiting', async () => {
      const pending = new Subject<Me>();
      api.login.mockReturnValue(pending);
      await fillValid();
      await submit();

      expect(button('Sign in').disabled).toBe(true);
      expect(button('Sign in').getAttribute('aria-busy')).toBe('true');
      button('Sign in').click();
      expect(api.login).toHaveBeenCalledTimes(1);
    });

    it('offers Google sign-in as a full-page navigation to the start URL', () => {
      const link = Array.from(root.querySelectorAll<HTMLAnchorElement>('a')).find(
        (a) => a.textContent?.trim() === 'Continue with Google',
      );
      expect(link?.getAttribute('href')).toBe('/api/auth/google/start');
    });

    it('links to registration and password recovery', () => {
      const hrefs = Array.from(root.querySelectorAll<HTMLAnchorElement>('a')).map((a) =>
        a.getAttribute('href'),
      );
      expect(hrefs).toContain('/register');
      expect(hrefs).toContain('/forgot-password');
    });

    it('shows "Invalid e-mail or password." in a role="alert" element on 401', async () => {
      failWith({ code: 'INVALID_CREDENTIALS', message: 'whatever the server says' });
      await fillValid();
      await submit();

      expect(alerts()).toContain(INVALID);
      expect(findButton('Resend verification e-mail')).toBeUndefined();
    });

    it('shows the throttle message on 429', async () => {
      failWith({ code: 'TOO_MANY_ATTEMPTS', message: 'x' });
      await fillValid();
      await submit();

      expect(alerts()).toContain(TOO_MANY);
    });

    it('offers to resend the verification e-mail on 403 EMAIL_NOT_VERIFIED', async () => {
      failWith({ code: 'EMAIL_NOT_VERIFIED', message: 'x' });
      await fillValid();
      await submit();

      expect(alerts()).toContain(NOT_VERIFIED);
      button('Resend verification e-mail').click();
      await render();

      expect(api.resendVerification).toHaveBeenCalledWith('ana@example.com');
      expect(root.textContent).toContain(
        'If the account needs verification, we sent a new e-mail.',
      );
    });

    it('reports a failed resend', async () => {
      failWith({ code: 'EMAIL_NOT_VERIFIED', message: 'x' });
      api.resendVerification.mockReturnValue(
        throwError(() => ({ code: 'NETWORK_ERROR', message: 'x' })),
      );
      await fillValid();
      await submit();
      button('Resend verification e-mail').click();
      await render();

      expect(alerts()).toContain('Unable to reach the server.');
    });

    it('falls back to a generic message for unexpected errors', async () => {
      failWith({ code: 'INTERNAL_ERROR', message: 'stack trace' });
      await fillValid();
      await submit();

      expect(alerts()).toContain('Something went wrong. Please try again.');
    });

    it('goes to /resumes after signing in when there is no returnUrl', async () => {
      await fillValid();
      await submit();

      expect(router.url).toBe('/resumes');
    });

    it('does not show the Google failure message without the query flag', () => {
      expect(root.textContent).not.toContain(GOOGLE_FAILED);
    });
  });

  it('shows the Google failure message when the query has error=google_failed', async () => {
    await setup('/login?error=google_failed');

    expect(alerts()).toContain(GOOGLE_FAILED);
  });

  it('returns to an internal returnUrl after signing in', async () => {
    await setup('/login?returnUrl=%2Fsessions%2F42%3Ftab%3Dreport');
    await fillValid();
    await submit();

    expect(router.url).toBe('/sessions/42?tab=report');
  });

  it('ignores an external returnUrl', async () => {
    await setup('/login?returnUrl=https%3A%2F%2Fevil.example%2F');
    await fillValid();
    await submit();

    expect(router.url).toBe('/resumes');
  });
});

describe('safeReturnUrl', () => {
  it.each([
    [null, '/resumes'],
    ['', '/resumes'],
    ['/sessions/1', '/sessions/1'],
    ['//evil.example', '/resumes'],
    ['/\\evil.example', '/resumes'],
    ['https://evil.example', '/resumes'],
    ['javascript:alert(1)', '/resumes'],
    ['/login', '/resumes'],
    ['/login?returnUrl=/x', '/resumes'],
  ])('maps %s to %s', (value, expected) => {
    expect(safeReturnUrl(value)).toBe(expected);
  });
});
