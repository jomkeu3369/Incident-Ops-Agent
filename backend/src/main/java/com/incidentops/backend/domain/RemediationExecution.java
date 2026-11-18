package com.incidentops.backend.domain;

import java.time.LocalDateTime;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class RemediationExecution {
    private Long id;
    private String approvalId;
    private String incidentCode;
    private String actionCommand;
    private String executedBy;
    private String executionStatus;
    private String executionOutput;
    private LocalDateTime executedAt;
}