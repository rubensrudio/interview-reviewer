import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Observable, Subject, of, throwError } from 'rxjs';

import { AuthApi, MessageResponse } from '../../core/auth/auth-api';
import { ApiError } from '../../core/http/api-error';
import { ForgotPasswordPage } from './forgot-password-page';

@Component({ template: '' })
class BlankPage {}

const NEUTRAL = 'If an account exists for this e-mail, we sent instructions to reset your password.';

class AuthApiStub {
  forgotPassword = vi.fn<(email: string) => Observable<MessageResponse>>(() =>
    of({ message: NEUTRAL }),
  );
}

describe('ForgotPasswordPage', () => {
  let fixture: ComponentFixture<ForgotPasswordPage>;
  let root: HTMLElement;
  let api: AuthApiStub;

  const alerts = (): string[] =>
    Array.from(root.querySelectorAll('[role="alert"]')).map((el) => el.textContent?.trim() ?? '');
  const submitButton = (): HTMLButtonElement => {
    const found = root.querySelector<HTMLButtonElement>('button[type="submit"]');
    if (!found) {
      throw new Error('Submit button not found');
    }
    return found;
  };
  const emailInput = (): HTMLInputElement => {
    const el = root.querySelector<HTMLInputElement>('#forgot-email');
    if (!el) {
      throw new Error('E-mail input not found');
    }
    return el;
  };
  const live = (): string =>
    root.querySelector('[aria-live="polite"]')?.textContent?.replace(/\s+/g, ' ').trim() ?? '';
  const render = async (): Promise<void> => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };
  const typeEmail = async (value: string): Promise<void> => {
    const el = emailInput();
    el.value = value;
    el.dispatchEvent(new Event('input'));
    await render();
  };
  const submit = async (): Promise<void> => {
    submitButton().click();
    await render();
  };

  const setup = async (configure?: (stub: AuthApiStub) => void): Promise<void> => {
    api = new AuthApiStub();
    configure?.(api);
    await TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'forgot-password', component: ForgotPasswordPage },
          { path: '**', component: BlankPage },
        ]),
        { provide: AuthApi, useValue: api },
      ],
    }).compileComponents();
    await TestBed.inject(Router).navigateByUrl('/forgot-password');
    fixture = TestBed.createComponent(ForgotPasswordPage);
    root = fixture.nativeElement as HTMLElement;
    await render();
  };

  const failWith = (err: ApiError) => (stub: AuthApiStub) =>
    stub.forgotPassword.mockReturnValue(throwError(() => err));

  it('renders a labelled e-mail field and a link back to sign in', async () => {
    await setup();

    const input = emailInput();
    expect(input.type).toBe('email');
    expect(input.getAttribute('autocomplete')).toBe('email');
    expect(input.getAttribute('maxlength')).toBe('1024');
    expect(input.required).toBe(true);
    expect(root.querySelector('label[for="forgot-email"]')?.textContent?.trim()).toBeTruthy();
    expect(root.querySelector('h1')?.textContent?.trim()).toBeTruthy();
    expect(root.querySelector('a[href="/login"]')).not.toBeNull();
  });

  it('shows the exact section 9 neutral message after submitting', async () => {
    await setup();

    await typeEmail('ana@example.com');
    await submit();

    expect(api.forgotPassword).toHaveBeenCalledTimes(1);
    expect(api.forgotPassword).toHaveBeenCalledWith('ana@example.com');
    expect(live()).toBe(NEUTRAL);
    expect(alerts()).toEqual([]);
  });

  it('shows the neutral answer from the backend unchanged', async () => {
    await setup((stub) =>
      stub.forgotPassword.mockReturnValue(of({ message: 'Backend neutral text.' })),
    );

    await typeEmail('ana@example.com');
    await submit();

    expect(live()).toBe('Backend neutral text.');
  });

  it('falls back to the section 9 neutral message when the backend sends none', async () => {
    await setup((stub) => stub.forgotPassword.mockReturnValue(of({ message: '' })));

    await typeEmail('ana@example.com');
    await submit();

    expect(live()).toBe(NEUTRAL);
  });

  it('disables the button while the request is in flight', async () => {
    const pending = new Subject<MessageResponse>();
    await setup((stub) => stub.forgotPassword.mockReturnValue(pending.asObservable()));

    await typeEmail('ana@example.com');
    await submit();

    expect(submitButton().disabled).toBe(true);
    expect(submitButton().getAttribute('aria-busy')).toBe('true');

    submitButton().click();
    await render();
    expect(api.forgotPassword).toHaveBeenCalledTimes(1);

    pending.next({ message: NEUTRAL });
    pending.complete();
    await render();

    expect(submitButton().disabled).toBe(false);
    expect(submitButton().getAttribute('aria-busy')).toBe('false');
    expect(live()).toBe(NEUTRAL);
  });

  it('flags an empty e-mail without calling the API and links the error', async () => {
    await setup();

    await submit();

    expect(api.forgotPassword).not.toHaveBeenCalled();
    const input = emailInput();
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(input.getAttribute('aria-describedby')).toBe('forgot-email-error');
    expect(root.querySelector('#forgot-email-error')?.getAttribute('role')).toBe('alert');
    expect(document.activeElement).toBe(input);
  });

  it('flags an over-long e-mail without calling the API', async () => {
    await setup();

    await typeEmail(`${'a'.repeat(1020)}@x.io`);
    await submit();

    expect(api.forgotPassword).not.toHaveBeenCalled();
    expect(root.querySelector('#forgot-email-error')?.textContent).toContain('1024');
  });

  it.each([
    ['TOO_MANY_ATTEMPTS', 'Too many attempts. Please try again later.'],
    ['VALIDATION_ERROR', 'Please check the highlighted fields.'],
    ['CSRF_FAILED', 'Your session expired. Please reload the page.'],
    ['NETWORK_ERROR', 'Unable to reach the server.'],
    ['SOMETHING_ELSE', 'Something went wrong. Please try again.'],
  ])('maps %s to the catalog text and re-enables the button', async (code, expected) => {
    await setup(failWith({ code, message: 'raw backend text' }));

    await typeEmail('ana@example.com');
    await submit();

    expect(alerts()).toContain(expected);
    expect(root.textContent).not.toContain('raw backend text');
    expect(live()).toBe('');
    expect(submitButton().disabled).toBe(false);
  });

  it('clears a previous error when submitting again', async () => {
    await setup(failWith({ code: 'NETWORK_ERROR', message: '' }));

    await typeEmail('ana@example.com');
    await submit();
    expect(alerts()).toContain('Unable to reach the server.');

    api.forgotPassword.mockReturnValue(of({ message: NEUTRAL }));
    await submit();

    expect(alerts()).toEqual([]);
    expect(live()).toBe(NEUTRAL);
  });
});
