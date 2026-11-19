package com.incidentops.backend.service;

import com.incidentops.backend.domain.Incident;
import com.incidentops.backend.domain.PostMortem;
import com.incidentops.backend.domain.RemediationApproval;
import com.incidentops.backend.domain.RemediationExecution;
import com.incidentops.backend.domain.RemediationPlan;
import com.incidentops.backend.domain.RemediationPlanStep;
import com.incidentops.backend.dto.ApprovalCreateRequest;
import com.incidentops.backend.dto.ApprovalDecisionRequest;
import com.incidentops.backend.dto.CompleteIncidentRequest;
import com.incidentops.backend.dto.CompleteIncidentResponse;
import com.incidentops.backend.dto.IncidentCreateRequest;
import com.incidentops.backend.dto.IncidentDetailResponse;
import com.incidentops.backend.dto.PlanCreateRequest;
import com.incidentops.backend.exception.ConflictException;
import com.incidentops.backend.exception.ResourceNotFoundException;
import com.incidentops.backend.mapper.CorpusMapper;
import com.incidentops.backend.mapper.IncidentMapper;
import com.incidentops.backend.mapper.PostMortemMapper;
import com.incidentops.backend.mapper.RemediationMapper;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Objects;
import java.util.Set;
import lombok.RequiredArgsConstructor;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@RequiredArgsConstructor
public class IncidentService {

    private final IncidentMapper incidentMapper;
    private final RemediationMapper remediationMapper;
    private final PostMortemMapper postMortemMapper;
    private final CorpusMapper corpusMapper;

    @Transactional(readOnly = true)
    public List<Incident> getAllIncidents() {
        return incidentMapper.findAll();
    }

    @Transactional(readOnly = true)
    public IncidentDetailResponse getIncidentDetail(String incidentCode) {
        Incident incident = getIncidentOrThrow(incidentCode);
        List<RemediationPlan> plans = remediationMapper.findPlansByIncidentCode(incidentCode);
        List<RemediationApproval> approvals = remediationMapper.findApprovalsByIncidentCode(incidentCode);
        List<RemediationExecution> executions = remediationMapper.findExecutionsByIncidentCode(incidentCode);
        PostMortem postMortem = postMortemMapper.findByIncidentCode(incidentCode);
        return new IncidentDetailResponse(incident, plans, approvals, executions, postMortem);
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
    public RemediationPlan createPlan(String incidentCode, PlanCreateRequest request) {
        Incident incident = lockIncident(incidentCode);
        requireUnresolved(incident);
        validatePlanSteps(incident, request.steps());
        RemediationPlan plan = RemediationPlan.builder()
                .planId(request.planId())
                .incidentCode(incident.getIncidentCode())
                .steps(request.steps())
                .verificationCriteria(request.verificationCriteria())
                .createdBy(request.createdBy())
                .build();
        try {
            remediationMapper.insertPlan(plan);
        } catch (DuplicateKeyException ex) {
            throw new ConflictException("이미 존재하는 계획 ID입니다: " + request.planId());
        }
        return remediationMapper.findPlanById(request.planId());
    }

    @Transactional
    public RemediationApproval createApproval(String incidentCode, ApprovalCreateRequest request) {
        Incident incident = lockIncident(incidentCode);
        requireUnresolved(incident);
        String canonicalCode = incident.getIncidentCode();
        if (remediationMapper.hasUnfinishedPlanApproval(canonicalCode)) {
            throw new ConflictException("처리되지 않은 복구 승인 요청이 있습니다.");
        }
        RemediationPlan plan = requireIncidentPlan(canonicalCode, request.planId());
        String actionCommand = plan.getSteps().stream()
                .filter(step -> Objects.equals(step.stepNo(), request.stepNo()))
                .map(RemediationPlanStep::actionCommand)
                .filter(command -> !isBlank(command))
                .findFirst()
                .orElseThrow(() -> new ConflictException("지정한 계획 단계에는 승인할 조치가 없습니다."));
        RemediationApproval approval = RemediationApproval.builder()
                .approvalId(request.approvalId())
                .incidentCode(canonicalCode)
                .planId(plan.getPlanId())
                .stepNo(request.stepNo())
                .actionCommand(actionCommand)
                .proposedBy(request.proposedBy())
                .decision("PENDING")
                .build();
        try {
            remediationMapper.insertApproval(approval);
        } catch (DuplicateKeyException ex) {
            throw new ConflictException("이미 존재하는 승인 ID입니다: " + request.approvalId());
        }
        incidentMapper.updateStatus(canonicalCode, "WAITING_APPROVAL");
        return remediationMapper.findApprovalById(request.approvalId());
    }

    @Transactional
    public RemediationApproval decideApproval(String incidentCode, String approvalId, ApprovalDecisionRequest request) {
        Incident incident = lockIncident(incidentCode);
        requireUnresolved(incident);
        RemediationApproval approval = remediationMapper.findApprovalById(approvalId);
        if (approval == null) {
            throw new ResourceNotFoundException("승인 요청을 찾을 수 없습니다: " + approvalId);
        }
        if (!incident.getIncidentCode().equals(approval.getIncidentCode())) {
            throw new ConflictException("승인 요청의 장애 코드가 일치하지 않습니다.");
        }
        if (!"PENDING".equals(approval.getDecision())) {
            throw new ConflictException("이미 처리된 승인 요청입니다: " + approval.getDecision());
        }
        String decision = request.decision().toUpperCase(Locale.ROOT);
        if (!"APPROVED".equals(decision) && !"REJECTED".equals(decision)) {
            throw new IllegalArgumentException("decision은 APPROVED 또는 REJECTED만 허용됩니다.");
        }
        if (approval.getPlanId() == null && "APPROVED".equals(decision)) {
            throw new ConflictException("계획이 없는 기존 요청은 새 승인 요청으로 교체해야 합니다.");
        }
        if (remediationMapper.updateApprovalDecision(approval.getApprovalId(), request.approvedBy(), decision) == 0) {
            throw new ConflictException("이미 처리된 승인 요청입니다.");
        }
        return remediationMapper.findApprovalById(approvalId);
    }

    @Transactional
    public CompleteIncidentResponse completeIncidentWithPostMortem(String incidentCode, CompleteIncidentRequest request) {
        Incident incident = lockIncident(incidentCode);
        String canonicalCode = incident.getIncidentCode();
        RemediationApproval approval = validateApproval(canonicalCode, request);
        String status = request.executionStatus().toUpperCase(Locale.ROOT);
        if (!"SUCCESS".equals(status) && !"FAILED".equals(status)) {
            throw new IllegalArgumentException("executionStatus는 SUCCESS 또는 FAILED만 허용됩니다.");
        }
        RemediationExecution existingExecution = remediationMapper.findExecutionByApprovalId(approval.getApprovalId());
        if (existingExecution != null) {
            if (!canonicalCode.equals(existingExecution.getIncidentCode())
                    || !sameCommand(existingExecution.getActionCommand(), approval.getActionCommand())
                    || !status.equals(existingExecution.getExecutionStatus())) {
                throw new ConflictException("이미 기록된 실행 결과와 완료 요청이 일치하지 않습니다.");
            }
            PostMortem existingPostMortem = "SUCCESS".equals(status)
                    ? postMortemMapper.findByIncidentCode(canonicalCode)
                    : null;
            return new CompleteIncidentResponse(
                    canonicalCode,
                    incident.getStatus(),
                    true,
                    existingExecution,
                    existingPostMortem
            );
        }

        requireUnresolved(incident);
        if (approval.getPlanId() == null) {
            throw new ConflictException("실행 결과를 기록하려면 계획에 연결된 새 승인이 필요합니다.");
        }
        if ("SUCCESS".equals(status)) {
            if (isBlank(request.verificationSummary())) {
                throw new IllegalArgumentException("SUCCESS 완료 처리 시 verificationSummary는 필수입니다.");
            }
            if (isBlank(request.rootCause()) || isBlank(request.timelineMarkdown()) || isBlank(request.preventionItems())) {
                throw new IllegalArgumentException("SUCCESS 완료 처리 시 rootCause, timelineMarkdown, preventionItems는 필수입니다.");
            }
        }

        RemediationExecution execution = RemediationExecution.builder()
                .approvalId(approval.getApprovalId())
                .incidentCode(canonicalCode)
                .actionCommand(approval.getActionCommand())
                .executedBy(request.executedBy())
                .executionStatus(status)
                .executionOutput(request.executionOutput())
                .verificationSummary(request.verificationSummary())
                .build();
        remediationMapper.insertExecution(execution);
        RemediationExecution savedExecution = remediationMapper.findExecutionByApprovalId(approval.getApprovalId());

        if ("FAILED".equals(status)) {
            return new CompleteIncidentResponse(
                    canonicalCode,
                    incident.getStatus(),
                    false,
                    savedExecution,
                    null
            );
        }

        String summary = isBlank(request.remediationSummary())
                ? approval.getActionCommand() + " 실행 완료"
                : request.remediationSummary();

        PostMortem postMortem = PostMortem.builder()
                .incidentCode(canonicalCode)
                .approvalId(approval.getApprovalId())
                .rootCause(request.rootCause())
                .timelineMarkdown(request.timelineMarkdown())
                .remediationSummary(summary)
                .preventionItems(request.preventionItems())
                .build();
        postMortemMapper.insert(postMortem);
        incidentMapper.updateStatus(canonicalCode, "RESOLVED");

        PostMortem savedPostMortem = postMortemMapper.findByIncidentCode(canonicalCode);
        return new CompleteIncidentResponse(
                canonicalCode,
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

    private void validatePlanSteps(Incident incident, List<RemediationPlanStep> steps) {
        Set<Integer> stepNumbers = new HashSet<>();
        Set<String> evidenceIds = new HashSet<>();
        int actionCount = 0;
        for (RemediationPlanStep step : steps) {
            if (!stepNumbers.add(step.stepNo())) {
                throw new IllegalArgumentException("복구 계획의 단계 번호는 중복될 수 없습니다.");
            }
            if (!isBlank(step.actionCommand())) {
                actionCount++;
            }
            evidenceIds.addAll(step.evidenceChunkIds());
        }
        if (actionCount > 1) {
            throw new IllegalArgumentException("복구 계획에서 승인할 수 있는 조치는 최대 하나입니다.");
        }
        if (!evidenceIds.isEmpty()) {
            List<String> allowedIds = corpusMapper.findScopedChunkIds(
                    incident.getIncidentCode(), incident.getServiceName(), evidenceIds);
            if (!new HashSet<>(allowedIds).containsAll(evidenceIds)) {
                throw new IllegalArgumentException("복구 계획의 근거는 해당 장애 로그 또는 해당 서비스 런북이어야 합니다.");
            }
        }
    }

    private RemediationApproval validateApproval(String incidentCode, CompleteIncidentRequest request) {
        RemediationApproval approval = remediationMapper.findApprovalById(request.approvalId());
        if (approval == null || !incidentCode.equals(approval.getIncidentCode())) {
            throw new ConflictException("해당 장애의 승인 요청을 찾을 수 없습니다.");
        }
        if (!"APPROVED".equals(approval.getDecision())) {
            throw new ConflictException("승인(APPROVED)되지 않은 요청입니다. 현재 상태: " + approval.getDecision());
        }
        if (!sameCommand(request.actionCommand(), approval.getActionCommand())) {
            throw new ConflictException("승인된 복구 명령어와 실행 명령어가 일치하지 않습니다.");
        }
        if (approval.getPlanId() != null) {
            requireIncidentPlan(incidentCode, approval.getPlanId());
        }
        return approval;
    }

    private RemediationPlan requireIncidentPlan(String incidentCode, String planId) {
        RemediationPlan plan = remediationMapper.findPlanById(planId);
        if (plan == null || !incidentCode.equals(plan.getIncidentCode())) {
            throw new ConflictException("해당 장애의 복구 계획을 찾을 수 없습니다.");
        }
        return plan;
    }

    private boolean sameCommand(String command, String approvedCommand) {
        return command.trim().equals(approvedCommand.trim());
    }

    private Incident lockIncident(String incidentCode) {
        return requireIncident(incidentCode, incidentMapper.lockByIncidentCode(incidentCode));
    }

    private void requireUnresolved(Incident incident) {
        if ("RESOLVED".equals(incident.getStatus())) {
            throw new ConflictException("이미 종료된 장애입니다.");
        }
    }

    private Incident getIncidentOrThrow(String incidentCode) {
        return requireIncident(incidentCode, incidentMapper.findByIncidentCode(incidentCode));
    }

    private Incident requireIncident(String incidentCode, Incident incident) {
        if (incident == null) {
            throw new ResourceNotFoundException("장애를 찾을 수 없습니다: " + incidentCode);
        }
        return incident;
    }

    private boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
