import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';

/** Policy version in force; must match the backend `privacy_version` setting. */
export const PRIVACY_VERSION = 'privacy-2026-09';

/**
 * Public privacy policy (DATA-08). Reachable with or without a session: no guard, no API call.
 * Static English text only, so nothing external is rendered.
 */
@Component({
  selector: 'app-privacy-page',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <article aria-labelledby="privacy-title">
      <h1 id="privacy-title">Privacy Policy</h1>
      <p class="version">Version {{ version }}</p>

      <section aria-labelledby="privacy-purpose">
        <h2 id="privacy-purpose">Purpose of processing</h2>
        <p>
          We process your resume, the job requirements you enter, your interview answers and the
          resulting reports only to run your interview practice: extracting your experience and
          skills, asking questions, evaluating your answers and showing your history.
        </p>
        <p>
          All processing runs on a language model hosted on the project's private infrastructure.
          Your data is never sent to external LLM providers or used in web searches.
        </p>
      </section>

      <section aria-labelledby="privacy-training">
        <h2 id="privacy-training">Model training and validation</h2>
        <p>
          Your data is not used to train the model, and it is not used to validate or test the
          model either. Validation and example sets never contain user resumes, requirements,
          answers or reports.
        </p>
      </section>

      <section aria-labelledby="privacy-retention">
        <h2 id="privacy-retention">Retention</h2>
        <ul>
          <li>Interview sessions that are not completed expire after 30 days without activity.</li>
          <li>Completed sessions, resumes and reports are kept until you delete them.</li>
          <li>Backups are kept for up to 30 days, so deleted data leaves backups within 30 days.</li>
        </ul>
      </section>

      <section aria-labelledby="privacy-deletion">
        <h2 id="privacy-deletion">How to delete your data</h2>
        <p>Deletion is immediate and permanent. While signed in, you can:</p>
        <ul>
          <li>
            delete a resume version on the <a routerLink="/resumes">Resumes</a> page; sessions that
            used it keep only the minimal snapshot (skills and cited evidence) until the session is
            deleted;
          </li>
          <li>
            delete an interview session, with its messages, answers and report, on the
            <a routerLink="/history">History</a> page;
          </li>
          <li>
            delete your whole account and all of its data on the
            <a routerLink="/account">Account</a> page.
          </li>
        </ul>
      </section>

      <p>See also the <a routerLink="/terms">Terms of Use</a>.</p>
    </article>
  `,
  styles: `
    :host {
      display: block;
      max-width: 42rem;
      margin: 0 auto;
      padding: 2rem 1rem;
      color: #1a1a1a;
      line-height: 1.5;
    }
    h1 {
      margin: 0 0 0.5rem;
      font-size: 1.75rem;
    }
    h2 {
      margin: 0 0 0.5rem;
      font-size: 1.25rem;
    }
    article {
      display: flex;
      flex-direction: column;
      gap: 1.5rem;
    }
    section {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }
    p,
    ul {
      margin: 0;
    }
    .version {
      color: #4b5563;
    }
    a {
      color: #1d4ed8;
    }
    a:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
  `,
})
export class PrivacyPage {
  protected readonly version = PRIVACY_VERSION;
}
