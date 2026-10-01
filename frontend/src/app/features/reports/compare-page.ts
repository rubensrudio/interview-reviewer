import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  OnInit,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Subscription } from 'rxjs';

import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';
import { Comparison, ReportApi } from './report-api';

const GENERIC_ERROR = 'Something went wrong. Please try again.';

/** Shown when the URL lacks `a` or `b`; same text as the history page hint. */
export const SELECT_TWO_MESSAGE = 'Select two completed interviews to compare them.';

/** CMP-02 warning, shown when the backend answers `comparable: false`. */
export const NOT_COMPARABLE_MESSAGE = 'These results are not directly comparable';

/** Catalog 8.3 texts for the errors of `GET /api/reports/compare`, keyed by `code`. */
const ERRORS: Readonly<Record<string, string>> = {
  REPORT_NOT_AVAILABLE: 'The report is not available for this interview.',
  RESOURCE_NOT_FOUND: 'Not found.',
  // The only parameters of this route are the two ids taken from the URL.
  VALIDATION_ERROR: SELECT_TWO_MESSAGE,
  [NETWORK_ERROR]: 'Unable to reach the server.',
};

/** Display labels for the `differences` values sent by the backend (plan 8.1). */
const DIFFERENCE_LABELS: Readonly<Record<string, string>> = {
  requirements: 'Confirmed requirements',
  questions: 'Questions',
  model_version: 'Model version',
  rubric_version: 'Rubric version',
};

interface ComparedIds {
  a: string;
  b: string;
}

/**
 * Side-by-side comparison of two completed sessions (CMP-01, CMP-02).
 *
 * Reads `a` and `b` from the query string and shows the `Comparison` (CT-64) exactly as received:
 * percentages and averages are decimal strings shown untouched, and which skills are common and
 * whether the sessions are comparable is decided by the backend.
 */
@Component({
  selector: 'app-compare-page',
  imports: [RouterLink],
  templateUrl: './compare-page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    :host {
      display: block;
      max-width: 48rem;
      margin: 0 auto;
      padding: 2rem 1rem;
      color: #1a1a1a;
    }
    h1 {
      margin: 0 0 1.5rem;
      font-size: 1.75rem;
    }
    h2 {
      margin: 0 0 0.75rem;
      font-size: 1.25rem;
    }
    p {
      margin: 0;
    }
    a {
      color: #1d4ed8;
    }
    section {
      margin-bottom: 1.5rem;
    }
    .error {
      padding: 0.75rem 1rem;
      border: 1px solid #b91c1c;
      border-radius: 0.375rem;
      color: #7f1d1d;
      background: #fef2f2;
    }
    .warning {
      margin-bottom: 1.5rem;
      padding: 1rem;
      border: 1px solid #92400e;
      border-radius: 0.5rem;
      color: #78350f;
      background: #fffbeb;
    }
    .warning p {
      font-weight: 600;
    }
    .warning ul {
      margin: 0.5rem 0 0;
      padding-left: 1.25rem;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      margin-top: 1rem;
    }
    .sides {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
      gap: 1rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .side {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      padding: 1rem;
      border: 1px solid #c4c4c4;
      border-radius: 0.5rem;
    }
    .side h3 {
      margin: 0;
      font-size: 1.05rem;
    }
    .percentage {
      font-size: 2rem;
      font-weight: 700;
    }
    .meta {
      color: #4b5563;
      font-size: 0.875rem;
      overflow-wrap: anywhere;
    }
    .side a {
      display: inline-flex;
      align-items: center;
      min-height: 2.75rem;
    }
    .table-wrap {
      overflow-x: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th,
    td {
      padding: 0.5rem;
      border-bottom: 1px solid #e5e7eb;
      text-align: left;
      overflow-wrap: anywhere;
    }
    tbody th {
      font-weight: 400;
    }
    button {
      display: inline-flex;
      align-items: center;
      min-height: 2.75rem;
      padding: 0.5rem 1rem;
      border-radius: 0.375rem;
      font: inherit;
      cursor: pointer;
    }
    .secondary {
      border: 1px solid #4b5563;
      color: #1a1a1a;
      background: #ffffff;
    }
    button:focus-visible,
    a:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
    }
  `,
})
export class ComparePage implements OnInit {
  private readonly api = inject(ReportApi);
  private readonly destroyRef = inject(DestroyRef);
  private readonly route = inject(ActivatedRoute);
  private ids: ComparedIds | null = null;
  private loadSub: Subscription | null = null;

  protected readonly comparison = signal<Comparison | null>(null);
  protected readonly loading = signal(false);
  protected readonly loadError = signal<string | null>(null);
  protected readonly missingIds = signal(false);

  protected readonly selectTwoMessage = SELECT_TWO_MESSAGE;
  protected readonly notComparableMessage = NOT_COMPARABLE_MESSAGE;

  ngOnInit(): void {
    this.destroyRef.onDestroy(() => this.loadSub?.unsubscribe());
    this.route.queryParamMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      const a = params.get('a') ?? '';
      const b = params.get('b') ?? '';
      if (this.ids && this.ids.a === a && this.ids.b === b) {
        return;
      }
      this.comparison.set(null);
      if (a === '' || b === '') {
        // Without both ids there is nothing to ask the backend for.
        this.loadSub?.unsubscribe();
        this.ids = null;
        this.loading.set(false);
        this.loadError.set(null);
        this.missingIds.set(true);
        return;
      }
      this.ids = { a, b };
      this.missingIds.set(false);
      this.load();
    });
  }

  protected reload(): void {
    this.load();
  }

  protected differenceLabel(code: string): string {
    return DIFFERENCE_LABELS[code] ?? code;
  }

  private load(): void {
    const ids = this.ids;
    if (!ids) {
      return;
    }
    this.loadSub?.unsubscribe();
    this.loading.set(true);
    this.loadError.set(null);
    this.loadSub = this.api.compare(ids.a, ids.b).subscribe({
      next: (comparison) => {
        this.loading.set(false);
        this.comparison.set(comparison);
      },
      error: (err: ApiError) => {
        this.loading.set(false);
        this.comparison.set(null);
        this.loadError.set(ERRORS[err.code] ?? GENERIC_ERROR);
      },
    });
  }
}
