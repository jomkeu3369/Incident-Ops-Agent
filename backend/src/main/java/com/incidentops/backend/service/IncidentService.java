package com.incidentops.backend.service;

import com.incidentops.backend.domain.Incident;
import com.incidentops.backend.domain.PostMortem;
import com.incidentops.backend.domain.RemediationApproval;
import com.incidentops.backend.domain.RemediationExecution;
import com.incidentops.backend.dto.ApprovalCreateRequest;
import com.incidentops.backend.dto.ApprovalDecisionRequest;
import com.incidentops.backend.dto.CompleteIncidentRequest;
import com.incidentops.backend.dto.CompleteIncidentResponse;
import com.incidentops.backend.dto.IncidentCreateRequest;
import com.incidentops.backend.dto.IncidentDetailResponse;
import com.incidentops.backend.exception.ConflictException;
import com.incidentops.backend.exception.ResourceNotFoundException;
import com.incidentops.backend.mapper.IncidentMapper;
import com.incidentops.backend.mapper.PostMortemMapper;
import com.incidentops.backend.mapper.RemediationMapper;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@RequiredArgsConstructor
public class IncidentService {

    private final IncidentMapper incidentMapper;
    private final RemediationMapper remediationMapper;
    private final PostMortemMapper postMortemMapper;

    @Transactional(readOnly = true)
    public List<Incident> getAllIncidents() {
        return incidentMapper.findAll();
    }

    @Transactional(readOnly = true)
    public IncidentDetailResponse getIncidentDetail(String incidentCode) {
        Incident incident = getIncidentOrThrow(incidentCode);
        List<RemediationApproval> approvals = remediationMapper.findApprovalsByIncidentCode(incidentCode);
        List<RemediationExecution> executions = remediationMapper.findExecutionsByIncidentCode(incidentCode);
        PostMortem postMortem = postMortemMapper.findByIncidentCode(incidentCode);
        return new IncidentDetailResponse(incident, approvals, executions, postMortem);
    }

    @Transactional
    public Incident createIncident(IncidentCreateRequest request) {
        if (incidentMapper.findByIncidentCode(request.incidentCode()) != null) {
            throw new ConflictException("이미 존재하는 장애 코드입니다: " + request.incidentCode());
        }
        Incident incident = Incident.builder()
                .incidentCode(request.incidentCode())
                .title(request.title())
                .serviceName(request.serviceName())
                .severity(request.severity().toUpperCase())
                .status("OPEN")
                .summary(request.summary())
                .build();
        incidentMapper.insert(incident);
        return incidentMapper.findByIncidentCode(request.incidentCode());
    }

    @Transactional
    public RemediationApproval createApproval(String incidentCode, ApprovalCreateRequest request) {
        getIncidentOrThrow(incidentCode);
        if (remediationMapper.findApprovalById(request.approvalId()) != null) {
            throw new ConflictException("이미 존재하는 승인 ID입니다: " + request.approvalId());
        }
        RemediationApproval approval = RemediationApproval.builder()
                .approvalId(request.approvalId())
                .incidentCode(incidentCode)
                .actionCommand(request.actionCommand())
                .proposedBy(request.proposedBy())
                .decision("PENDING")
                .build();
        remediationMapper.insertApproval(approval);
        incidentMapper.updateStatus(incidentCode, "WAITING_APPROVAL");
        return remediationMapper.findApprovalById(request.approvalId());
    }

    @Transactional
    public RemediationApproval decideApproval(String incidentCode, String approvalId, ApprovalDecisionRequest request) {
        getIncidentOrThrow(incidentCode);
        RemediationApproval approval = remediationMapper.findApprovalById(approvalId);
        if (approval == null) {
            throw new ResourceNotFoundException("승인 요청을 찾을 수 없습니다: " + approvalId);
        }
        if (!incidentCode.equals(approval.getIncidentCode())) {
            throw new ConflictException("승인 요청의 장애 코드가 일치하지 않습니다.");
        }
        if (!"PENDING".equals(approval.getDecision())) {
            throw new ConflictException("이미 처리된 승인 요청입니다: " + approval.getDecision());
        }
        String decision = request.decision().toUpperCase();
        if (!"APPROVED".equals(decision) && !"REJECTED".equals(decision)) {
            throw new IllegalArgumentException("decision은 APPROVED 또는 REJECTED만 허용됩니다.");
        }
        remediationMapper.updateApprovalDecision(approvalId, request.approvedBy(), decision);
        return remediationMapper.findApprovalById(approvalId);
    }

    @Transactional
    public CompleteIncidentResponse completeIncidentWithPostMortem(String incidentCode, CompleteIncidentRequest request) {
        Incident incident = getIncidentOrThrow(incidentCode);

        // 멱등성 선검사: 동일 approvalId 실행 이력이 이미 존재하면 기존 결과 반환
        RemediationExecution existingExecution = remediationMapper.findExecutionByApprovalId(request.approvalId());
        if (existingExecution != null) {
            PostMortem existingPostMortem = postMortemMapper.findByIncidentCode(incidentCode);
            return new CompleteIncidentResponse(
                    incidentCode,
                    incident.getStatus(),
                    true,
                    existingExecution,
                    existingPostMortem
            );
        }

        // 승인 원장 서버 측 검증 (불일치 시 409 Conflict)
        RemediationApproval approval = remediationMapper.findApprovalById(request.approvalId());
        if (approval == null) {
            throw new ConflictException("존재하지 않는 승인 ID입니다: " + request.approvalId());
        }
        if (!"APPROVED".equals(approval.getDecision())) {
            throw new ConflictException("승인(APPROVED)되지 않은 요청입니다. 현재 상태: " + approval.getDecision());
        }
        if (!incidentCode.equals(approval.getIncidentCode())) {
            throw new ConflictException("승인 원장의 장애 코드와 일치하지 않습니다.");
        }
        if (!request.actionCommand().trim().equals(approval.getActionCommand().trim())) {
            throw new ConflictException("승인된 복구 명령어와 실행 명령어가 일치하지 않습니다.");
        }

        String status = request.executionStatus().toUpperCase();
        if (!"SUCCESS".equals(status) && !"FAILED".equals(status)) {
            throw new IllegalArgumentException("executionStatus는 SUCCESS 또는 FAILED만 허용됩니다.");
        }

        // 실행 이력 적재
        RemediationExecution execution = RemediationExecution.builder()
                .approvalId(request.approvalId())
                .incidentCode(incidentCode)
                .actionCommand(request.actionCommand())
                .executedBy(request.executedBy())
                .executionStatus(status)
                .executionOutput(request.executionOutput())
                .build();
        remediationMapper.insertExecution(execution);
        RemediationExecution savedExecution = remediationMapper.findExecutionByApprovalId(request.approvalId());

        if ("FAILED".equals(status)) {
            return new CompleteIncidentResponse(
                    incidentCode,
                    incident.getStatus(),
                    false,
                    savedExecution,
                    null
            );
        }

        // SUCCESS 인 경우 단일 트랜잭션 내에서 포스트모템 생성 및 장애 RESOLVED 전환
        if (isBlank(request.rootCause()) || isBlank(request.timelineMarkdown()) || isBlank(request.preventionItems())) {
            throw new IllegalArgumentException("SUCCESS 완료 처리 시 rootCause, timelineMarkdown, preventionItems는 필수입니다.");
        }
        String summary = isBlank(request.remediationSummary())
                ? request.actionCommand() + " 실행 완료"
                : request.remediationSummary();

        PostMortem postMortem = PostMortem.builder()
                .incidentCode(incidentCode)
                .approvalId(request.approvalId())
                .rootCause(request.rootCause())
                .timelineMarkdown(request.timelineMarkdown())
                .remediationSummary(summary)
                .preventionItems(request.preventionItems())
                .build();
        postMortemMapper.insert(postMortem);
        incidentMapper.updateStatus(incidentCode, "RESOLVED");

        PostMortem savedPostMortem = postMortemMapper.findByIncidentCode(incidentCode);
        return new CompleteIncidentResponse(
                incidentCode,
                "RESOLVED",
                false,
                savedExecution,
                savedPostMortem
        );
    }

    @Transactional(readOnly = true)
    public PostMortem getPostMortem(String incidentCode) {
        getIncidentOrThrow(incidentCode);
        PostMortem postMortem = postMortemMapper.findByIncidentCode(incidentCode);
        if (postMortem == null) {
            throw new ResourceNotFoundException("포스트모템이 아직 생성되지 않았습니다: " + incidentCode);
        }
        return postMortem;
    }

    private Incident getIncidentOrThrow(String incidentCode) {
        Incident incident = incidentMapper.findByIncidentCode(incidentCode);
        if (incident == null) {
            throw new ResourceNotFoundException("장애를 찾을 수 없습니다: " + incidentCode);
        }
        return incident;
    }

    private boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}