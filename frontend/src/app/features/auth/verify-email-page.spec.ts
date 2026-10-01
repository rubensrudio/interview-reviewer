import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Observable, Subject, of, throwError } from 'rxjs';

import { AuthApi, MessageResponse } from '../../core/auth/auth-api';
import { ApiError } from '../../core/http/api-error';
import { VerifyEmailPage } from './verify-email-page';

@Component({ template: '' })
class BlankPage {}

const LINK_INVALID_MESSAGE = 'This link is invalid or has expired. Request a new one.';
const RESEND_SENT = 'If the account needs verification, we sent a new e-mail.';

class AuthApiStub {
  verifyEmail = vi.fn<(token: string) => Observable<MessageResponse>>(() =>
    of({ message: 'Your e-mail has been verified.' }),
  );
  resendVerification = vi.fn<(email: string) => Observable<MessageResponse>>(() =>
    of({ message: RESEND_SENT }),
  );
}

describe('VerifyEmailPage', () => {
  let fixture: ComponentFixture<VerifyEmailPage>;
  let root: HTMLElement;
  let api: AuthApiStub;

  const text = (): string => root.textContent?.replace(/\s+/g, ' ').trim() ?? '';
  const alerts = (): string[] =>
    Array.from(root.querySelectorAll('[role="alert"]')).map((el) => el.textContent?.trim() ?? '');
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
  const emailInput = (): HTMLInputElement | null =>
    root.querySelector<HTMLInputElement>('#verify-email');
  const loginLink = (): HTMLAnchorElement | null =>
    root.querySelector<HTMLAnchorElement>('a[href="/login"]');
  const render = async (): Promise<void> => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };
  const typeEmail = async (value: string): Promise<void> => {
    const el = emailInput();
    if (!el) {
      throw new Error('E-mail input not found');
    }
    el.value = value;
    el.dispatchEvent(new Event('input'));
    await render();
  };

  const setup = async (url: string, configure?: (stub: AuthApiStub) => void): Promise<void> => {
    api = new AuthApiStub();
    configure?.(api);
    await TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'verify-email', component: VerifyEmailPage },
          { path: '**', component: BlankPage },
        ]),
        { provide: AuthApi, useValue: api },
      ],
    }).compileComponents();
    await TestBed.inject(Router).navigateByUrl(url);
    fixture = TestBed.createComponent(VerifyEmailPage);
    root = fixture.nativeElement as HTMLElement;
    await render();
  };

  const failVerifyWith = (err: ApiError) => (stub: AuthApiStub) =>
    stub.verifyEmail.mockReturnValue(throwError(() => err));

  describe('valid link', () => {
    it('sends the token from the URL and shows a link to /login on success', async () => {
      await setup('/verify-email?token=abc123');

      expect(api.verifyEmail).toHaveBeenCalledTimes(1);
      expect(api.verifyEmail).toHaveBeenCalledWith('abc123');
      expect(text()).toContain('Your e-mail has been verified.');
      expect(loginLink()).not.toBeNull();
      expect(emailInput()).toBeNull();
      expect(root.querySelector('[role="status"]')?.textContent).toContain(
        'Your e-mail has been verified.',
      );
    });

    it('moves focus to the result heading for screen reader users', async () => {
      await setup('/verify-email?token=abc123');

      const heading = root.querySelector('h1');
      expect(heading?.getAttribute('tabindex')).toBe('-1');
      expect(document.activeElement).toBe(heading);
    });

    it('falls back to a default success text when the backend sends no message', async () => {
      await setup('/verify-email?token=abc123', (stub) =>
        stub.verifyEmail.mockReturnValue(of({ message: '' })),
      );

      expect(text()).toContain('Your e-mail has been verified. You can now sign in.');
    });

    it('shows a busy status while the verification is in flight', async () => {
      const pending = new Subject<MessageResponse>();
      await setup('/verify-email?token=abc123', (stub) =>
        stub.verifyEmail.mockReturnValue(pending.asObservable()),
      );

      const status = root.querySelector('[role="status"]');
      expect(status?.textContent).toContain('Verifying your e-mail');
      expect(root.querySelector('[aria-busy="true"]')).not.toBeNull();
      expect(loginLink()).toBeNull();

      pending.next({ message: 'Verified.' });
      pending.complete();
      await render();
      expect(loginLink()).not.toBeNull();
    });
  });

  describe('invalid link (AUTH-94)', () => {
    it('shows the section 9 message and the resend form on 400 LINK_INVALID', async () => {
      await setup(
        '/verify-email?token=used',
        failVerifyWith({ code: 'LINK_INVALID', message: 'backend text' }),
      );

      expect(alerts()).toContain(LINK_INVALID_MESSAGE);
      expect(text()).not.toContain('backend text');
      const input = emailInput();
      expect(input).not.toBeNull();
      expect(input?.type).toBe('email');
      expect(input?.getAttribute('autocomplete')).toBe('email');
      expect(input?.getAttribute('maxlength')).toBe('1024');
      expect(root.querySelector('label[for="verify-email"]')?.textContent?.trim()).toBeTruthy();
      expect(findButton('Send a new link')).toBeDefined();
    });

    it('treats a missing token as an invalid link without calling the API', async () => {
      await setup('/verify-email');

      expect(api.verifyEmail).not.toHaveBeenCalled();
      expect(alerts()).toContain(LINK_INVALID_MESSAGE);
      expect(emailInput()).not.toBeNull();
    });

    it('treats an over-long token as an invalid link without calling the API', async () => {
      await setup(`/verify-email?token=${'a'.repeat(1025)}`);

      expect(api.verifyEmail).not.toHaveBeenCalled();
      expect(alerts()).toContain(LINK_INVALID_MESSAGE);
    });

    it('requests a new link for the typed e-mail and announces the neutral answer', async () => {
      await setup(
        '/verify-email?token=used',
        failVerifyWith({ code: 'LINK_INVALID', message: '' }),
      );

      await typeEmail('ana@example.com');
      button('Send a new link').click();
      await render();

      expect(api.resendVerification).toHaveBeenCalledWith('ana@example.com');
      const live = root.querySelector('[aria-live="polite"]');
      expect(live?.textContent).toContain(RESEND_SENT);
    });

    it('flags an empty e-mail without calling the API and links the error', async () => {
      await setup(
        '/verify-email?token=used',
        failVerifyWith({ code: 'LINK_INVALID', message: '' }),
      );

      button('Send a new link').click();
      await render();

      expect(api.resendVerification).not.toHaveBeenCalled();
      const input = emailInput();
      expect(input?.getAttribute('aria-invalid')).toBe('true');
      expect(input?.getAttribute('aria-describedby')).toBe('verify-email-error');
      expect(root.querySelector('#verify-email-error')?.getAttribute('role')).toBe('alert');
      expect(document.activeElement).toBe(input);
    });

    it('maps resend failures to the catalog text', async () => {
      await setup('/verify-email?token=used', (stub) => {
        stub.verifyEmail.mockReturnValue(
          throwError((): ApiError => ({ code: 'LINK_INVALID', message: '' })),
        );
        stub.resendVerification.mockReturnValue(
          throwError((): ApiError => ({ code: 'NETWORK_ERROR', message: 'x' })),
        );
      });

      await typeEmail('ana@example.com');
      button('Send a new link').click();
      await render();

      expect(alerts()).toContain('Unable to reach the server.');
      expect(button('Send a new link').disabled).toBe(false);
    });

    it('ignores repeated clicks while a resend is in flight', async () => {
      const pending = new Subject<MessageResponse>();
      await setup('/verify-email?token=used', (stub) => {
        stub.verifyEmail.mockReturnValue(
          throwError((): ApiError => ({ code: 'LINK_INVALID', message: '' })),
        );
        stub.resendVerification.mockReturnValue(pending.asObservable());
      });

      await typeEmail('ana@example.com');
      button('Send a new link').click();
      await render();
      const send = button('Send a new link');
      expect(send.disabled).toBe(true);
      expect(send.getAttribute('aria-busy')).toBe('true');
      send.click();

      expect(api.resendVerification).toHaveBeenCalledTimes(1);
    });
  });

  describe('other failures', () => {
    it('shows the network text and lets the user retry', async () => {
      await setup(
        '/verify-email?token=abc123',
        failVerifyWith({ code: 'NETWORK_ERROR', message: 'x' }),
      );

      expect(alerts()).toContain('Unable to reach the server.');
      expect(emailInput()).toBeNull();

      api.verifyEmail.mockReturnValue(of({ message: 'Verified.' }));
      button('Try again').click();
      await render();

      expect(api.verifyEmail).toHaveBeenCalledTimes(2);
      expect(api.verifyEmail).toHaveBeenLastCalledWith('abc123');
      expect(loginLink()).not.toBeNull();
    });

    it('shows a generic text for unexpected codes', async () => {
      await setup(
        '/verify-email?token=abc123',
        failVerifyWith({ code: 'HTTP_ERROR', message: 'Internal Server Error' }),
      );

      expect(alerts()).toContain('Something went wrong. Please try again.');
      expect(text()).not.toContain('Internal Server Error');
    });
  });
});
