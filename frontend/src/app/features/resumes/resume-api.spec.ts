import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { firstValueFrom } from 'rxjs';

import { ApiError } from '../../core/http/api-error';
import { ExtractionItem, ResumeApi, ResumeDetail, ResumeSummary } from './resume-api';

const SUMMARY: ResumeSummary = {
  id: 'r-1',
  filename: 'cv.pdf',
  uploaded_at: '2026-10-01T12:00:00Z',
  status: 'ready',
  failure_code: null,
  failure_message: null,
};

const ITEM: ExtractionItem = {
  id: 'i-1',
  kind: 'skill',
  fields: { name: 'TypeScript' },
  origin: 'explicit',
  evidence: ['TypeScript'],
};

const DETAIL: ResumeDetail = { ...SUMMARY, items: [ITEM] };

describe('ResumeApi', () => {
  let api: ResumeApi;
  let httpTesting: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    api = TestBed.inject(ResumeApi);
    httpTesting = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpTesting.verify());

  it('list() GETs /api/resumes without a status filter', async () => {
    const result = firstValueFrom(api.list());

    const req = httpTesting.expectOne((r) => r.url === '/api/resumes');
    expect(req.request.method).toBe('GET');
    expect(req.request.params.has('status')).toBe(false);
    req.flush([SUMMARY]);

    await expect(result).resolves.toEqual([SUMMARY]);
  });

  it('list("ready") GETs /api/resumes?status=ready', async () => {
    const result = firstValueFrom(api.list('ready'));

    const req = httpTesting.expectOne('/api/resumes?status=ready');
    expect(req.request.method).toBe('GET');
    req.flush([SUMMARY]);

    await expect(result).resolves.toEqual([SUMMARY]);
  });

  it('get() GETs the resume detail', async () => {
    const result = firstValueFrom(api.get('r-1'));

    const req = httpTesting.expectOne('/api/resumes/r-1');
    expect(req.request.method).toBe('GET');
    req.flush(DETAIL);

    await expect(result).resolves.toEqual(DETAIL);
  });

  it('get() encodes the id in the URL', () => {
    void firstValueFrom(api.get('a/b')).catch(() => undefined);

    httpTesting.expectOne('/api/resumes/a%2Fb').flush(DETAIL);
  });

  it('upload() POSTs a FormData with the "file" field to /api/resumes', async () => {
    const file = new File(['%PDF-1.7'], 'cv.pdf', { type: 'application/pdf' });
    const result = firstValueFrom(api.upload(file));

    const req = httpTesting.expectOne('/api/resumes');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toBeInstanceOf(FormData);
    const body = req.request.body as FormData;
    const sent = body.get('file');
    expect(sent).toBeInstanceOf(File);
    expect((sent as File).name).toBe('cv.pdf');
    req.flush({ ...SUMMARY, status: 'received' }, { status: 201, statusText: 'Created' });

    await expect(result).resolves.toEqual({ ...SUMMARY, status: 'received' });
  });

  it('upload() rejects with a normalized ApiError', async () => {
    const file = new File(['x'], 'big.pdf', { type: 'application/pdf' });
    const result = firstValueFrom(api.upload(file));

    httpTesting.expectOne('/api/resumes').flush(
      {
        error: {
          code: 'FILE_TOO_LARGE',
          message: 'The file is larger than 5 MB.',
          details: { limit_bytes: 5242880 },
        },
      },
      { status: 413, statusText: 'Payload Too Large' },
    );

    await expect(result).rejects.toMatchObject({
      code: 'FILE_TOO_LARGE',
      details: { limit_bytes: 5242880 },
    } satisfies Partial<ApiError>);
  });

  it('addItem() POSTs kind and fields to the items endpoint', async () => {
    const result = firstValueFrom(api.addItem('r-1', { kind: 'skill', fields: { name: 'Go' } }));

    const req = httpTesting.expectOne('/api/resumes/r-1/items');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ kind: 'skill', fields: { name: 'Go' } });
    req.flush(ITEM, { status: 201, statusText: 'Created' });

    await expect(result).resolves.toEqual(ITEM);
  });

  it('updateItem() PATCHes the fields of one item', async () => {
    const result = firstValueFrom(api.updateItem('r-1', 'i-1', { fields: { name: 'Rust' } }));

    const req = httpTesting.expectOne('/api/resumes/r-1/items/i-1');
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body).toEqual({ fields: { name: 'Rust' } });
    req.flush(ITEM);

    await expect(result).resolves.toEqual(ITEM);
  });

  it('removeItem() DELETEs one item', async () => {
    const result = firstValueFrom(api.removeItem('r-1', 'i-1'));

    const req = httpTesting.expectOne('/api/resumes/r-1/items/i-1');
    expect(req.request.method).toBe('DELETE');
    req.flush(null, { status: 204, statusText: 'No Content' });

    await expect(result).resolves.toBeUndefined();
  });

  it('delete() DELETEs the resume', async () => {
    const result = firstValueFrom(api.delete('r-1'));

    const req = httpTesting.expectOne('/api/resumes/r-1');
    expect(req.request.method).toBe('DELETE');
    req.flush(null, { status: 204, statusText: 'No Content' });

    await expect(result).resolves.toBeUndefined();
  });

  it('delete() rejects with RESOURCE_NOT_FOUND for a foreign resume', async () => {
    const result = firstValueFrom(api.delete('r-2'));

    httpTesting
      .expectOne('/api/resumes/r-2')
      .flush(
        { error: { code: 'RESOURCE_NOT_FOUND', message: 'Not found.', details: null } },
        { status: 404, statusText: 'Not Found' },
      );

    await expect(result).rejects.toMatchObject({
      code: 'RESOURCE_NOT_FOUND',
    } satisfies Partial<ApiError>);
  });
});
