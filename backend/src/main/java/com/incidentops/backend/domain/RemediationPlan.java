package com.incidentops.backend.domain;

import java.time.LocalDateTime;
import java.util.List;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class RemediationPlan {
    private Long id;
    private String planId;
    private String incidentCode;
    private List<RemediationPlanStep> steps;
    private String verificationCriteria;
    private String createdBy;
    private LocalDateTime createdAt;
}
