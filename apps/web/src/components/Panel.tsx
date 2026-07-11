import type { ReactNode } from "react";

import styles from "./Panel.module.css";

interface PanelProps {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
  testId?: string;
}

export function Panel({ title, actions, children, testId }: PanelProps): JSX.Element {
  return (
    <section className={styles.panel} data-testid={testId}>
      <header className={styles.header}>
        <h2 className={styles.title}>{title}</h2>
        {actions ? <div className={styles.actions}>{actions}</div> : null}
      </header>
      <div className={styles.body}>{children}</div>
    </section>
  );
}
