-- Existing decisions stay in the audit history without a fabricated plan or step.
ALTER TABLE remediation_approvals
    ADD COLUMN plan_id VARCHAR(64) NULL,
    ADD COLUMN step_no INT NULL,
    ADD CONSTRAINT fk_remediation_approvals_plan
        FOREIGN KEY (plan_id) REFERENCES remediation_plans (plan_id);
