import { HttpErrorResponse, HttpHeaders } from '@angular/common/http';
import { toApiError } from './api-error';

describe('toApiError', () => {
  it('returns the code, message and details from the CT-3 envelope', () => {
    const err = new HttpErrorResponse({
      status: 422,
      error: {
        error: {
          code: 'PASSWORD_POLICY',
          message: 'Password must have at least 8 characters.',
          details: { violations: ['too_short'] },
        },
      },
    });

    expect(toApiError(err)).toEqual({
      code: 'PASSWORD_POLICY',
      message: 'Password must have at least 8 characters.',
      details: { violations: ['too_short'] },
    });
  });

  it('omits details when the envelope carries null details', () => {
    const err = new HttpErrorResponse({
      status: 401,
      error: {
        error: { code: 'AUTH_REQUIRED', message: 'Authentication required.', details: null },
      },
    });

    expect(toApiError(err)).toEqual({ code: 'AUTH_REQUIRED', message: 'Authentication required.' });
  });

  it('parses an envelope delivered as a JSON string', () => {
    const err = new HttpErrorResponse({
      status: 409,
      error: JSON.stringify({ error: { code: 'INVALID_STATE', message: 'Nope.', details: null } }),
    });

    expect(toApiError(err).code).toBe('INVALID_STATE');
  });

  it('maps a network failure (status 0) to NETWORK_ERROR', () => {
    const err = new HttpErrorResponse({ status: 0, error: new ProgressEvent('error') });

    expect(toApiError(err).code).toBe('NETWORK_ERROR');
  });

  it('tolerates the Starlette {"detail": ...} body outside the envelope', () => {
    const err = new HttpErrorResponse({
      status: 405,
      statusText: 'Method Not Allowed',
      headers: new HttpHeaders(),
      error: { detail: 'Method Not Allowed' },
    });

    expect(toApiError(err)).toEqual({
      code: 'HTTP_ERROR',
      message: 'Method Not Allowed',
      details: { status: 405, detail: 'Method Not Allowed' },
    });
  });

  it('maps a Starlette 404 to RESOURCE_NOT_FOUND', () => {
    const err = new HttpErrorResponse({ status: 404, error: { detail: 'Not Found' } });

    expect(toApiError(err).code).toBe('RESOURCE_NOT_FOUND');
  });

  it('maps a non-JSON server error to HTTP_ERROR with the status', () => {
    const err = new HttpErrorResponse({ status: 502, statusText: 'Bad Gateway', error: '<html>' });

    expect(toApiError(err)).toEqual({
      code: 'HTTP_ERROR',
      message: 'Bad Gateway',
      details: { status: 502 },
    });
  });

  it('maps any other value to UNKNOWN_ERROR', () => {
    expect(toApiError(new Error('boom'))).toEqual({ code: 'UNKNOWN_ERROR', message: 'boom' });
    expect(toApiError(undefined).code).toBe('UNKNOWN_ERROR');
  });

  it('returns an ApiError unchanged', () => {
    const apiError = { code: 'SESSION_CLOSED', message: 'This interview is closed.' };

    expect(toApiError(apiError)).toEqual(apiError);
  });
});
