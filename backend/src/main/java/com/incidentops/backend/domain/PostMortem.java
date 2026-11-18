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
public class PostMortem {
    private Long id;
    private String incidentCode;
    private String approvalId;
    private String rootCause;
    private String timelineMarkdown;
    private String remediationSummary;
    private String preventionItems;
    private LocalDateTime createdAt;
}