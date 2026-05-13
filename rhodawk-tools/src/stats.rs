//! Project statistics and health report module for rhodawk-tools.
//!
//! Aggregates scan and analysis data to produce a unified project
//! health score (0-100) with actionable recommendations for improvement.
//! Combines outputs from scanner, analyzer, and search modules.

use serde::{Deserialize, Serialize};
use std::path::Path;

use crate::analyzer::{self, AnalysisReport};
use crate::scanner::{self, ScanReport};
use crate::search;

/// Comprehensive project health report.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ProjectReport {
    /// Name of the project (derived from directory name).
    pub project_name: String,
    /// File scan results.
    pub scan: ScanReport,
    /// Code analysis results.
    pub analysis: AnalysisReport,
    /// Overall health score from 0 to 100.
    pub health_score: f64,
    /// Actionable recommendations for improving project health.
    pub recommendations: Vec<String>,
}

/// Generate a comprehensive project health report.
///
/// Combines scan results, code analysis, and pattern detection to
/// produce a health score and recommendations. The health score is
/// calculated based on test presence, complexity, CI configuration,
/// documentation, and security patterns.
///
/// # Arguments
///
/// * `path` - Root directory to generate stats for
/// * `json_output` - If true, output JSON; otherwise print human-readable report
///
/// # Returns
///
/// `Ok(())` on success, `Err` with description on failure.
pub fn generate_report(path: &Path, json_output: bool) -> Result<(), String> {
    if !path.exists() {
        return Err(format!("Path does not exist: {}", path.display()));
    }

    let report = build_project_report(path)?;

    if json_output {
        let json = serde_json::to_string_pretty(&report)
            .map_err(|e| format!("JSON serialization error: {}", e))?;
        println!("{}", json);
    } else {
        print_stats_report(&report);
    }

    Ok(())
}

/// Build a complete ProjectReport for the given path.
fn build_project_report(path: &Path) -> Result<ProjectReport, String> {
    let project_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("unknown")
        .to_string();

    let scan = scanner::build_scan_report(path)?;
    let analysis = analyzer::build_analysis_report(path, None)?;

    let (health_score, recommendations) = calculate_health(&scan, &analysis, path);

    Ok(ProjectReport {
        project_name,
        scan,
        analysis,
        health_score,
        recommendations,
    })
}

/// Calculate health score and generate recommendations.
///
/// Health score is computed on a 0-100 scale based on:
/// - +20 if has test files (test_files > 0)
/// - +20 if test ratio > 0.3 (test_files / source_files)
/// - +20 if average complexity < 10
/// - +20 if has CI config (.github/workflows/ exists)
/// - +10 if has README.md
/// - +10 if max complexity < 30
/// - -10 if any secret patterns found
/// - -10 if no dependency lock file
fn calculate_health(
    scan: &ScanReport,
    analysis: &AnalysisReport,
    path: &Path,
) -> (f64, Vec<String>) {
    let mut score: f64 = 0.0;
    let mut recommendations: Vec<String> = Vec::new();

    let has_tests = !scan.test_files.is_empty();
    let source_files = scan.total_files.saturating_sub(scan.config_files.len());
    let test_ratio = if source_files > 0 {
        scan.test_files.len() as f64 / source_files as f64
    } else {
        0.0
    };

    // +20 if has tests
    if has_tests {
        score += 20.0;
    } else {
        recommendations.push("Add test files to improve code reliability".to_string());
    }

    // +20 if test ratio > 0.3
    if test_ratio > 0.3 {
        score += 20.0;
    } else if has_tests {
        recommendations.push(format!(
            "Improve test coverage (current ratio: {:.1}%, target: 30%+)",
            test_ratio * 100.0
        ));
    }

    // +20 if average complexity < 10
    if analysis.avg_complexity < 10.0 {
        score += 20.0;
    } else {
        recommendations.push(format!(
            "Reduce average complexity (current: {:.1}, target: < 10)",
            analysis.avg_complexity
        ));
    }

    // +20 if has CI config
    let has_ci = has_ci_config(path);
    if has_ci {
        score += 20.0;
    } else {
        recommendations.push("Add CI pipeline (.github/workflows/) for automated testing".to_string());
    }

    // +10 if has README
    let has_readme = path.join("README.md").exists() || path.join("readme.md").exists();
    if has_readme {
        score += 10.0;
    } else {
        recommendations.push("Add a README.md for project documentation".to_string());
    }

    // +10 if max complexity < 30
    if analysis.max_complexity < 30 {
        score += 10.0;
    } else {
        // Find the hotspot file names for the recommendation
        let hotspot_names: Vec<String> = analysis
            .hotspots
            .iter()
            .filter(|h| h.complexity >= 30)
            .take(3)
            .map(|h| {
                let short = h.file.rsplit('/').next().unwrap_or(&h.file);
                format!("{} (complexity: {})", short, h.complexity)
            })
            .collect();
        recommendations.push(format!(
            "Reduce complexity in: {}",
            hotspot_names.join(", ")
        ));
    }

    // -10 if secrets found
    let has_secrets = search::has_secrets(path);
    if has_secrets {
        score -= 10.0;
        recommendations.push("Remove hardcoded secrets and use environment variables".to_string());
    }

    // -10 if no dependency lock file
    let has_lock_file = has_dependency_lock(path);
    if !has_lock_file {
        score -= 10.0;
        recommendations.push(
            "Add a dependency lock file (package-lock.json, Cargo.lock, etc.)".to_string(),
        );
    }

    // Clamp score to 0-100
    score = score.clamp(0.0, 100.0);

    (score, recommendations)
}

/// Check if the project has CI configuration.
fn has_ci_config(path: &Path) -> bool {
    let github_workflows = path.join(".github").join("workflows");
    let gitlab_ci = path.join(".gitlab-ci.yml");
    let circle_ci = path.join(".circleci");
    let jenkins = path.join("Jenkinsfile");

    github_workflows.exists() || gitlab_ci.exists() || circle_ci.exists() || jenkins.exists()
}

/// Check if the project has a dependency lock file.
fn has_dependency_lock(path: &Path) -> bool {
    let lock_files = [
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "go.sum",
        "poetry.lock",
        "Pipfile.lock",
        "Gemfile.lock",
    ];

    for lock_file in &lock_files {
        if path.join(lock_file).exists() {
            return true;
        }
    }
    false
}

/// Print a human-readable stats report.
fn print_stats_report(report: &ProjectReport) {
    println!("=== Rhodawk Project Report: {} ===", report.project_name);
    println!();
    println!("Health Score: {:.0}/100", report.health_score);
    println!();
    println!("--- Overview ---");
    println!("Total files:     {}", report.scan.total_files);
    println!("Total lines:     {}", report.scan.total_lines);
    println!("Languages:       {}", report.scan.languages.len());
    println!("Config files:    {}", report.scan.config_files.len());
    println!("Test files:      {}", report.scan.test_files.len());
    println!("Dependency files:{}", report.scan.dependency_files.len());
    println!();
    println!("--- Complexity ---");
    println!("Files analyzed:  {}", report.analysis.files_analyzed);
    println!("Total functions: {}", report.analysis.total_functions);
    println!("Total classes:   {}", report.analysis.total_classes);
    println!("Avg complexity:  {:.2}", report.analysis.avg_complexity);
    println!("Max complexity:  {}", report.analysis.max_complexity);
    println!();

    if !report.recommendations.is_empty() {
        println!("--- Recommendations ---");
        for (i, rec) in report.recommendations.iter().enumerate() {
            println!("  {}. {}", i + 1, rec);
        }
    } else {
        println!("No recommendations - project is in good health!");
    }

    println!();
    if !report.analysis.hotspots.is_empty() {
        println!("--- Top Hotspots ---");
        for hotspot in report.analysis.hotspots.iter().take(5) {
            let short_path = hotspot.file.rsplit('/').next().unwrap_or(&hotspot.file);
            println!(
                "  {} (complexity: {}, functions: {}, lines: {})",
                short_path, hotspot.complexity, hotspot.function_count, hotspot.line_count
            );
        }
    }
}
