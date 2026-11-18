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
public class RemediationApproval {
    private Long id;
    private String approvalId;
    private String incidentCode;
    private String actionCommand;
    private String proposedBy;
    private String approvedBy;
    private String decision;
    private LocalDateTime createdAt;
    private LocalDateTime decidedAt;
}