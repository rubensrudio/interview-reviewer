import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { Observable, Subject, of, throwError } from 'rxjs';

import { AuthApi, MessageResponse, RegisterResponse } from '../../core/auth/auth-api';
import { ApiError } from '../../core/http/api-error';
import { RegisterPage } from './register-page';

@Component({ template: '' })
class BlankPage {}

const NEUTRAL =
  'Check your inbox to continue. If you already have an account, sign in or reset your password.';
const POLICY = 'Password must have at least 8 characters and must not be a common password.';

class AuthApiStub {
  register = vi.fn<(email: string, password: string) => Observable<RegisterResponse>>(() =>
    of({ message: NEUTRAL, email_delivery: 'sent' }),
  );
  resendVerification = vi.fn<(email: string) => Observable<MessageResponse>>(() =>
    of({ message: 'If the account needs verification, we sent a new e-mail.' }),
  );
}

describe('RegisterPage', () => {
  let fixture: ComponentFixture<RegisterPage>;
  let root: HTMLElement;
  let api: AuthApiStub;

  const input = (id: string): HTMLInputElement => {
    const el = root.querySelector<HTMLInputElement>(`#${id}`);
    if (!el) {
      throw new Error(`Input #${id} not found`);
    }
    return el;
  };
  const button = (label: string): HTMLButtonElement => {
    const found = Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => b.textContent?.trim() === label,
    );
    if (!found) {
      throw new Error(`Button "${label}" not found`);
    }
    return found;
  };
  const type = (id: string, value: string): void => {
    const el = input(id);
    el.value = value;
    el.dispatchEvent(new Event('input'));
  };
  const check = (id: string, checked = true): void => {
    const el = input(id);
    el.checked = checked;
    el.dispatchEvent(new Event('change'));
  };
  const render = async (): Promise<void> => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };
  const fillValid = async (): Promise<void> => {
    type('register-email', 'ana@example.com');
    type('register-password', 'correct horse battery');
    check('register-accept');
    await render();
  };
  const submit = async (): Promise<void> => {
    button('Create account').click();
    await render();
  };

  beforeEach(async () => {
    api = new AuthApiStub();
    await TestBed.configureTestingModule({
      imports: [RegisterPage],
      providers: [
        provideRouter([{ path: '**', component: BlankPage }]),
        { provide: AuthApi, useValue: api },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(RegisterPage);
    root = fixture.nativeElement as HTMLElement;
    await render();
  });

  it('labels every field and caps them at 1024 characters', () => {
    for (const id of ['register-email', 'register-password']) {
      const label = root.querySelector(`label[for="${id}"]`);
      expect(label?.textContent?.trim()).toBeTruthy();
      expect(input(id).getAttribute('maxlength')).toBe('1024');
    }
    expect(input('register-email').type).toBe('email');
    expect(input('register-password').type).toBe('password');
    expect(input('register-email').getAttribute('autocomplete')).toBe('email');
    expect(input('register-password').getAttribute('autocomplete')).toBe('new-password');
  });

  it('links the consent checkbox to the terms and the privacy policy', () => {
    const label = root.querySelector('label[for="register-accept"]');
    const links = Array.from(label?.querySelectorAll<HTMLAnchorElement>('a') ?? []);
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/terms', '/privacy']);
  });

  it('keeps submission disabled while the consent is not checked', async () => {
    type('register-email', 'ana@example.com');
    type('register-password', 'correct horse battery');
    await render();

    expect(button('Create account').disabled).toBe(true);
    button('Create account').click();
    await render();
    expect(api.register).not.toHaveBeenCalled();

    check('register-accept');
    await render();
    expect(button('Create account').disabled).toBe(false);
  });

  it('flags empty fields without calling the API', async () => {
    check('register-accept');
    await render();
    await submit();

    expect(api.register).not.toHaveBeenCalled();
    expect(input('register-email').getAttribute('aria-invalid')).toBe('true');
    expect(input('register-password').getAttribute('aria-invalid')).toBe('true');
    const emailError = root.querySelector(
      `#${input('register-email').getAttribute('aria-describedby')}`,
    );
    expect(emailError?.textContent?.trim()).toBe('Enter your e-mail.');
  });

  it('sends the e-mail and password as typed', async () => {
    await fillValid();
    await submit();

    expect(api.register).toHaveBeenCalledWith('ana@example.com', 'correct horse battery');
  });

  it('shows the neutral message after a 202 and hides the form', async () => {
    await fillValid();
    await submit();

    const status = root.querySelector('[role="status"]');
    expect(status?.textContent).toContain('Check your inbox to continue.');
    expect(root.querySelector('form')).toBeNull();
    expect(root.textContent).not.toContain('may take a while');
    expect(root.querySelector('a[href="/login"]')).not.toBeNull();
  });

  it('shows the PASSWORD_POLICY message next to the password field', async () => {
    const error: ApiError = {
      code: 'PASSWORD_POLICY',
      message: POLICY,
      details: { violations: ['too_short'] },
    };
    api.register.mockReturnValue(throwError(() => error));
    await fillValid();
    await submit();

    const password = input('register-password');
    expect(password.getAttribute('aria-invalid')).toBe('true');
    const describedBy = password.getAttribute('aria-describedby');
    expect(describedBy).toBeTruthy();
    const hint = root.querySelector(`#${describedBy}`);
    expect(hint?.textContent?.trim()).toBe(POLICY);
    expect(hint?.getAttribute('role')).toBe('alert');
    expect(root.querySelector('form')).not.toBeNull();
  });

  it('clears the password policy error when the password changes', async () => {
    api.register.mockReturnValue(
      throwError((): ApiError => ({ code: 'PASSWORD_POLICY', message: POLICY })),
    );
    await fillValid();
    await submit();

    type('register-password', 'another passphrase');
    await render();
    expect(root.textContent).not.toContain(POLICY);
    expect(input('register-password').getAttribute('aria-invalid')).toBe('false');
  });

  it('shows a form-level alert for TERMS_NOT_ACCEPTED', async () => {
    api.register.mockReturnValue(
      throwError((): ApiError => ({ code: 'TERMS_NOT_ACCEPTED', message: 'x' })),
    );
    await fillValid();
    await submit();

    const alert = root.querySelector('.form-error[role="alert"]');
    expect(alert?.textContent?.trim()).toBe(
      'You must accept the Terms of Use and the Privacy Policy.',
    );
  });

  it('shows a generic alert on network failure and allows retrying', async () => {
    api.register.mockReturnValue(
      throwError((): ApiError => ({
        code: 'NETWORK_ERROR',
        message: 'Unable to reach the server.',
      })),
    );
    await fillValid();
    await submit();

    const alert = root.querySelector('.form-error[role="alert"]');
    expect(alert?.textContent?.trim()).toBe('Unable to reach the server.');
    expect(button('Create account').disabled).toBe(false);
  });

  it('disables the form while the request is pending', async () => {
    const pending = new Subject<RegisterResponse>();
    api.register.mockReturnValue(pending.asObservable());
    await fillValid();
    await submit();

    const submitButton = root.querySelector<HTMLButtonElement>('button[type="submit"]');
    expect(submitButton?.disabled).toBe(true);
    expect(submitButton?.getAttribute('aria-busy')).toBe('true');
    submitButton?.click();
    expect(api.register).toHaveBeenCalledTimes(1);

    pending.next({ message: NEUTRAL, email_delivery: 'sent' });
    pending.complete();
    await render();
    expect(root.querySelector('[role="status"]')?.textContent).toContain(NEUTRAL);
  });

  describe('when e-mail delivery is delayed', () => {
    beforeEach(async () => {
      api.register.mockReturnValue(of({ message: NEUTRAL, email_delivery: 'delayed' }));
      await fillValid();
      await submit();
    });

    it('warns that the e-mail may take a while and offers a resend', () => {
      const status = root.querySelector('[role="status"]');
      expect(status?.textContent).toContain('Check your inbox to continue.');
      expect(status?.textContent).toContain('The e-mail may take a while to arrive.');
      expect(button('Resend verification e-mail')).toBeTruthy();
    });

    it('resends to the registered e-mail and shows the backend message', async () => {
      button('Resend verification e-mail').click();
      await render();

      expect(api.resendVerification).toHaveBeenCalledWith('ana@example.com');
      expect(root.textContent).toContain(
        'If the account needs verification, we sent a new e-mail.',
      );
    });

    it('announces a resend failure', async () => {
      api.resendVerification.mockReturnValue(
        throwError((): ApiError => ({
          code: 'NETWORK_ERROR',
          message: 'Unable to reach the server.',
        })),
      );
      button('Resend verification e-mail').click();
      await render();

      const alert = root.querySelector('.resend-error[role="alert"]');
      expect(alert?.textContent?.trim()).toBe('Unable to reach the server.');
      expect(button('Resend verification e-mail').disabled).toBe(false);
    });
  });
});
