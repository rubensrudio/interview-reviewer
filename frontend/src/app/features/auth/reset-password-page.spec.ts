import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Observable, Subject, of, throwError } from 'rxjs';

import { AuthApi } from '../../core/auth/auth-api';
import { ApiError } from '../../core/http/api-error';
import { ResetPasswordPage } from './reset-password-page';

@Component({ template: '' })
class BlankPage {}

const TOKEN = 'reset-token-123';
const WEAK = 'Password must have at least 8 characters and must not be a common password.';
const LINK_INVALID_TEXT = 'This link is invalid or has expired. Request a new one.';

class AuthApiStub {
  resetPassword = vi.fn<(token: string, newPassword: string) => Observable<void>>(() =>
    of(undefined),
  );
}

describe('ResetPasswordPage', () => {
  let fixture: ComponentFixture<ResetPasswordPage>;
  let root: HTMLElement;
  let api: AuthApiStub;
  let router: Router;

  const alerts = (): string[] =>
    Array.from(root.querySelectorAll('[role="alert"]')).map(
      (el) => el.textContent?.replace(/\s+/g, ' ').trim() ?? '',
    );
  const passwordInput = (): HTMLInputElement | null =>
    root.querySelector<HTMLInputElement>('#reset-password');
  const requirePasswordInput = (): HTMLInputElement => {
    const el = passwordInput();
    if (!el) {
      throw new Error('Password input not found');
    }
    return el;
  };
  const submitButton = (): HTMLButtonElement => {
    const found = root.querySelector<HTMLButtonElement>('button[type="submit"]');
    if (!found) {
      throw new Error('Submit button not found');
    }
    return found;
  };
  const render = async (): Promise<void> => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };
  const typePassword = async (value: string): Promise<void> => {
    const el = requirePasswordInput();
    el.value = value;
    el.dispatchEvent(new Event('input'));
    await render();
  };
  const submit = async (): Promise<void> => {
    submitButton().click();
    await render();
  };

  const setup = async (
    url = `/reset-password?token=${TOKEN}`,
    configure?: (stub: AuthApiStub) => void,
  ): Promise<void> => {
    api = new AuthApiStub();
    configure?.(api);
    await TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'reset-password', component: ResetPasswordPage },
          { path: '**', component: BlankPage },
        ]),
        { provide: AuthApi, useValue: api },
      ],
    }).compileComponents();
    router = TestBed.inject(Router);
    await router.navigateByUrl(url);
    fixture = TestBed.createComponent(ResetPasswordPage);
    root = fixture.nativeElement as HTMLElement;
    await render();
  };

  const failWith = (err: ApiError) => (stub: AuthApiStub) =>
    stub.resetPassword.mockReturnValue(throwError(() => err));

  it('renders a labelled new-password field and never renders the token', async () => {
    await setup();

    const input = requirePasswordInput();
    expect(input.type).toBe('password');
    expect(input.getAttribute('autocomplete')).toBe('new-password');
    expect(input.getAttribute('maxlength')).toBe('1024');
    expect(input.required).toBe(true);
    expect(root.querySelector('label[for="reset-password"]')?.textContent?.trim()).toBeTruthy();
    expect(root.querySelector('h1')?.textContent?.trim()).toBeTruthy();
    expect(root.innerHTML).not.toContain(TOKEN);
  });

  it('204 sends the token and the new password, then navigates to /login', async () => {
    await setup();

    await typePassword('a-strong-passphrase');
    await submit();

    expect(api.resetPassword).toHaveBeenCalledTimes(1);
    expect(api.resetPassword).toHaveBeenCalledWith(TOKEN, 'a-strong-passphrase');
    expect(router.url).toBe('/login');
  });

  it('400 LINK_INVALID shows the catalog text with a link to /forgot-password', async () => {
    await setup(undefined, failWith({ code: 'LINK_INVALID', message: 'raw backend text' }));

    await typePassword('a-strong-passphrase');
    await submit();

    expect(alerts()).toContain(LINK_INVALID_TEXT);
    expect(root.querySelector('a[href="/forgot-password"]')).not.toBeNull();
    expect(root.textContent).not.toContain('raw backend text');
    expect(passwordInput()).toBeNull();
    expect(router.url).not.toBe('/login');
  });

  it('a missing token shows the invalid-link state without calling the API', async () => {
    await setup('/reset-password');

    expect(api.resetPassword).not.toHaveBeenCalled();
    expect(alerts()).toContain(LINK_INVALID_TEXT);
    expect(root.querySelector('a[href="/forgot-password"]')).not.toBeNull();
    expect(passwordInput()).toBeNull();
  });

  it('422 PASSWORD_POLICY shows the weak password message inline on the field', async () => {
    await setup(
      undefined,
      failWith({ code: 'PASSWORD_POLICY', message: 'raw', details: { violations: ['common'] } }),
    );

    await typePassword('password');
    await submit();

    const input = requirePasswordInput();
    const error = root.querySelector('#reset-password-error');
    expect(error?.textContent?.trim()).toBe(WEAK);
    expect(error?.getAttribute('role')).toBe('alert');
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(input.getAttribute('aria-describedby')).toBe('reset-password-error');
    expect(document.activeElement).toBe(input);
    expect(router.url).not.toBe('/login');
  });

  it('clears the weak password message once the password changes', async () => {
    await setup(undefined, failWith({ code: 'PASSWORD_POLICY', message: '' }));

    await typePassword('password');
    await submit();
    expect(alerts()).toContain(WEAK);

    await typePassword('password-2');

    expect(alerts()).not.toContain(WEAK);
    expect(requirePasswordInput().getAttribute('aria-invalid')).toBe('false');
  });

  it('flags an empty password without calling the API', async () => {
    await setup();

    await submit();

    expect(api.resetPassword).not.toHaveBeenCalled();
    const input = requirePasswordInput();
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(input.getAttribute('aria-describedby')).toBe('reset-password-error');
    expect(document.activeElement).toBe(input);
  });

  it('flags an over-long password without calling the API', async () => {
    await setup();

    await typePassword('a'.repeat(1025));
    await submit();

    expect(api.resetPassword).not.toHaveBeenCalled();
    expect(root.querySelector('#reset-password-error')?.textContent).toContain('1024');
  });

  it('does not judge password strength on the client', async () => {
    await setup();

    await typePassword('1');
    await submit();

    expect(api.resetPassword).toHaveBeenCalledWith(TOKEN, '1');
  });

  it('disables the button while the request is in flight', async () => {
    const pending = new Subject<void>();
    await setup(undefined, (stub) => stub.resetPassword.mockReturnValue(pending.asObservable()));

    await typePassword('a-strong-passphrase');
    await submit();

    expect(submitButton().disabled).toBe(true);
    expect(submitButton().getAttribute('aria-busy')).toBe('true');
    submitButton().click();
    await render();
    expect(api.resetPassword).toHaveBeenCalledTimes(1);

    pending.error({ code: 'NETWORK_ERROR', message: '' } satisfies ApiError);
    await render();

    expect(submitButton().disabled).toBe(false);
    expect(alerts()).toContain('Unable to reach the server.');
  });

  it.each([
    ['VALIDATION_ERROR', 'Please check the highlighted fields.'],
    ['CSRF_FAILED', 'Your session expired. Please reload the page.'],
    ['NETWORK_ERROR', 'Unable to reach the server.'],
    ['SOMETHING_ELSE', 'Something went wrong. Please try again.'],
  ])('maps %s to the catalog text and keeps the form', async (code, expected) => {
    await setup(undefined, failWith({ code, message: 'raw backend text' }));

    await typePassword('a-strong-passphrase');
    await submit();

    expect(alerts()).toContain(expected);
    expect(root.textContent).not.toContain('raw backend text');
    expect(passwordInput()).not.toBeNull();
    expect(submitButton().disabled).toBe(false);
  });
});
