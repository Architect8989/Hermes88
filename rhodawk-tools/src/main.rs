//! Rhodawk Tools - Fast CLI for repository analysis, code metrics, and pattern matching.
//!
//! This binary provides four subcommands:
//! - `scan`: Parallel file scanning with language detection
//! - `analyze`: Code complexity analysis and hotspot identification
//! - `search`: Structural pattern matching (functions, classes, TODOs, secrets)
//! - `stats`: Aggregated project health report with recommendations

mod scanner;
mod analyzer;
mod search;
mod stats;

use clap::{Parser, Subcommand};
use std::path::PathBuf;
use std::process;

/// Rhodawk Tools - Fast repository analysis and code intelligence CLI.
///
/// Designed for integration with Hermes88 AI assistant to provide
/// rapid codebase understanding, complexity metrics, and pattern detection.
#[derive(Parser, Debug)]
#[command(name = "rhodawk-tools")]
#[command(version = "0.1.0")]
#[command(about = "Fast CLI tools for repository analysis, code metrics, and pattern matching")]
#[command(author = "Rhodawk AI <engineering@rhodawkai.com>")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

/// Available subcommands for rhodawk-tools.
#[derive(Subcommand, Debug)]
enum Commands {
    /// Scan a directory for file statistics and language breakdown.
    ///
    /// Traverses the directory tree in parallel, detecting languages by
    /// file extension, counting lines, and identifying config/test/dependency files.
    Scan(ScanArgs),

    /// Analyze code complexity and identify hotspots.
    ///
    /// Calculates approximate cyclomatic complexity by counting branching
    /// keywords per file, identifies the most complex files as hotspots.
    Analyze(AnalyzeArgs),

    /// Search for patterns in the codebase.
    ///
    /// Supports built-in pattern types (function, class, todo, secret) and
    /// custom regex patterns. Outputs matches with file path and line number.
    Search(SearchArgs),

    /// Generate a comprehensive project health report.
    ///
    /// Combines scan and analysis data to produce a health score (0-100)
    /// with actionable recommendations for improving project quality.
    Stats(StatsArgs),
}

/// Arguments for the `scan` subcommand.
#[derive(Parser, Debug)]
struct ScanArgs {
    /// Path to the directory to scan.
    #[arg(short, long)]
    path: PathBuf,

    /// Output results as JSON instead of human-readable format.
    #[arg(short, long, default_value_t = false)]
    json: bool,
}

/// Arguments for the `analyze` subcommand.
#[derive(Parser, Debug)]
struct AnalyzeArgs {
    /// Path to the directory to analyze.
    #[arg(short, long)]
    path: PathBuf,

    /// Optional language filter (e.g., "python", "typescript", "go", "rust").
    #[arg(short, long)]
    language: Option<String>,

    /// Output results as JSON instead of human-readable format.
    #[arg(short, long, default_value_t = false)]
    json: bool,
}

/// Arguments for the `search` subcommand.
#[derive(Parser, Debug)]
struct SearchArgs {
    /// Path to the directory to search.
    #[arg(short, long)]
    path: PathBuf,

    /// Regex pattern for custom searches (used when --type is "custom").
    #[arg(long, default_value = "")]
    pattern: String,

    /// Type of search to perform: function, class, todo, secret, custom.
    #[arg(short = 't', long = "type", default_value = "custom")]
    search_type: String,

    /// Output results as JSON instead of human-readable format.
    #[arg(short, long, default_value_t = false)]
    json: bool,
}

/// Arguments for the `stats` subcommand.
#[derive(Parser, Debug)]
struct StatsArgs {
    /// Path to the directory to generate stats for.
    #[arg(short, long)]
    path: PathBuf,

    /// Output results as JSON instead of human-readable format.
    #[arg(short, long, default_value_t = false)]
    json: bool,
}

/// Entry point for rhodawk-tools CLI.
///
/// Parses command-line arguments using clap and dispatches to the appropriate
/// subcommand handler. Exits with code 1 on any error.
fn main() {
    let cli = Cli::parse();

    let result = match cli.command {
        Commands::Scan(args) => {
            scanner::scan_directory(&args.path, args.json)
        }
        Commands::Analyze(args) => {
            analyzer::analyze_project(&args.path, args.language.as_deref(), args.json)
        }
        Commands::Search(args) => {
            let search_type = search::SearchType::from_str(&args.search_type);
            search::search_patterns(&args.path, &args.pattern, search_type, args.json)
        }
        Commands::Stats(args) => {
            stats::generate_report(&args.path, args.json)
        }
    };

    if let Err(e) = result {
        eprintln!("Error: {}", e);
        process::exit(1);
    }
}
