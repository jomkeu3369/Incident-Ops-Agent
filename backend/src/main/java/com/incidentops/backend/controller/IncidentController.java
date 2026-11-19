package com.incidentops.backend.controller;

import com.incidentops.backend.domain.Incident;
import com.incidentops.backend.domain.PostMortem;
import com.incidentops.backend.domain.RemediationApproval;
import com.incidentops.backend.domain.RemediationPlan;
import com.incidentops.backend.dto.ApprovalCreateRequest;
import com.incidentops.backend.dto.ApprovalDecisionRequest;
import com.incidentops.backend.dto.CompleteIncidentRequest;
import com.incidentops.backend.dto.CompleteIncidentResponse;
import com.incidentops.backend.dto.IncidentCreateRequest;
import com.incidentops.backend.dto.IncidentDetailResponse;
import com.incidentops.backend.dto.PlanCreateRequest;
import com.incidentops.backend.service.IncidentService;
import jakarta.validation.Valid;
import java.net.URI;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.util.UriComponentsBuilder;

@RestController
@RequestMapping("/api/incidents")
@RequiredArgsConstructor
public class IncidentController {

    private final IncidentService incidentService;

    @GetMapping
    public ResponseEntity<List<Incident>> getAllIncidents() {
        return ResponseEntity.ok(incidentService.getAllIncidents());
    }

    @GetMapping("/{incidentCode}")
    public ResponseEntity<IncidentDetailResponse> getIncidentDetail(@PathVariable String incidentCode) {
        return ResponseEntity.ok(incidentService.getIncidentDetail(incidentCode));
    }

    @PostMapping
    public ResponseEntity<Incident> createIncident(@Valid @RequestBody IncidentCreateRequest request) {
        Incident created = incidentService.createIncident(request);
        return ResponseEntity
                .created(URI.create("/api/incidents/" + created.getIncidentCode()))
                .body(created);
    }

    @PostMapping("/{incidentCode}/approvals")
    public ResponseEntity<RemediationApproval> createApproval(
            @PathVariable String incidentCode,
            @Valid @RequestBody ApprovalCreateRequest request) {
        RemediationApproval approval = incidentService.createApproval(incidentCode, request);
        return ResponseEntity
                .created(UriComponentsBuilder.fromPath("/api/incidents/{incidentCode}/approvals/{approvalId}")
                        .encode()
                        .buildAndExpand(incidentCode, approval.getApprovalId())
                        .toUri())
                .body(approval);
    }

    @PostMapping("/{incidentCode}/plans")
    public ResponseEntity<RemediationPlan> createPlan(
            @PathVariable String incidentCode,
            @Valid @RequestBody PlanCreateRequest request) {
        return ResponseEntity.status(HttpStatus.CREATED).body(incidentService.createPlan(incidentCode, request));
    }

    @PatchMapping("/{incidentCode}/approvals/{approvalId}")
    public ResponseEntity<RemediationApproval> decideApproval(
            @PathVariable String incidentCode,
            @PathVariable String approvalId,
            @Valid @RequestBody ApprovalDecisionRequest request) {
        return ResponseEntity.ok(incidentService.decideApproval(incidentCode, approvalId, request));
    }

    @PostMapping("/{incidentCode}/complete")
    public ResponseEntity<CompleteIncidentResponse> completeIncident(
            @PathVariable String incidentCode,
            @Valid @RequestBody CompleteIncidentRequest request) {
        return ResponseEntity.ok(incidentService.completeIncidentWithPostMortem(incidentCode, request));
    }

    @GetMapping("/{incidentCode}/postmortem")
    public ResponseEntity<PostMortem> getPostMortem(@PathVariable String incidentCode) {
        return ResponseEntity.ok(incidentService.getPostMortem(incidentCode));
    }
}
