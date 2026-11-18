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
public class Incident {
    private Long id;
    private String incidentCode;
    private String title;
    private String serviceName;
    private String severity;
    private String status;
    private String summary;
    private LocalDateTime createdAt;
    private LocalDateTime updatedAt;
}