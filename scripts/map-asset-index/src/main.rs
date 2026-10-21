use anyhow::{bail, Context, Result};
use retoc::{ser::Readable, EIoChunkType, FileReaderTrait, PakFileReader, Toc};
use std::collections::BTreeSet;
use std::env;
use std::fs::{self, File};
use std::io::BufReader;
use std::path::{Path, PathBuf};

fn collect_archives(path: &Path, tocs: &mut Vec<PathBuf>, paks: &mut Vec<PathBuf>) -> Result<()> {
    if path.is_file() {
        if let Some(ext) = path.extension() {
            if ext.eq_ignore_ascii_case("utoc") {
                tocs.push(path.to_path_buf());
            } else if ext.eq_ignore_ascii_case("pak") {
                paks.push(path.to_path_buf());
            }
        }
        return Ok(());
    }

    if !path.is_dir() {
        bail!(
            "input does not exist or is not readable: {}",
            path.display()
        );
    }

    for entry in fs::read_dir(path).with_context(|| format!("reading {}", path.display()))? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        let child = entry.path();
        if file_type.is_dir() {
            collect_archives(&child, tocs, paks)?;
        } else if file_type.is_file() {
            if let Some(ext) = child.extension() {
                if ext.eq_ignore_ascii_case("utoc") {
                    tocs.push(child);
                } else if ext.eq_ignore_ascii_case("pak") {
                    paks.push(child);
                }
            }
        }
    }
    Ok(())
}

fn cooked_map_path(path: &str) -> Option<(String, String)> {
    let normalized = path.replace('\\', "/");
    let (_, content_path) = normalized.split_once("/Content/")?;
    let package_path = content_path.strip_suffix(".umap")?;
    let map_id = package_path.rsplit('/').next()?;
    if map_id.is_empty() {
        return None;
    }
    Some((map_id.to_owned(), format!("/Game/{package_path}.{map_id}")))
}

fn main() -> Result<()> {
    let inputs: Vec<PathBuf> = env::args_os().skip(1).map(PathBuf::from).collect();
    if inputs.is_empty() {
        bail!("usage: dune-map-asset-index <Content/Paks directory or .utoc/.pak file> [...]");
    }

    let mut toc_paths = Vec::new();
    let mut pak_paths = Vec::new();
    for input in &inputs {
        collect_archives(input, &mut toc_paths, &mut pak_paths)?;
    }
    toc_paths.sort();
    toc_paths.dedup();
    pak_paths.sort();
    pak_paths.dedup();
    if toc_paths.is_empty() && pak_paths.is_empty() {
        bail!("no .utoc or .pak files found in the supplied inputs");
    }

    let mut maps = BTreeSet::new();
    let has_iostore_tocs = !toc_paths.is_empty();
    for toc_path in toc_paths {
        let mut reader = BufReader::new(
            File::open(&toc_path).with_context(|| format!("opening {}", toc_path.display()))?,
        );
        let toc =
            Toc::de(&mut reader).with_context(|| format!("parsing {}", toc_path.display()))?;
        for chunk in &toc.chunks {
            if chunk.get_chunk_type() != EIoChunkType::ExportBundleData {
                continue;
            }
            if let Some(package_path) = toc.file_name(*chunk) {
                if let Some((map_id, asset_path)) = cooked_map_path(&package_path) {
                    maps.insert((map_id, asset_path));
                }
            }
        }
    }

    let skipped_paks = if has_iostore_tocs {
        if !pak_paths.is_empty() {
            eprintln!(
                "skipping {} PAK archive(s); indexing IoStore .utoc files",
                pak_paths.len()
            );
        }
        pak_paths.len()
    } else {
        for pak_path in pak_paths {
            let reader = PakFileReader::new(&pak_path)
                .with_context(|| format!("opening {}", pak_path.display()))?;
            for cooked_path in reader.list_files()? {
                if let Some((map_id, asset_path)) = cooked_map_path(&cooked_path.to_string()) {
                    maps.insert((map_id, asset_path));
                }
            }
        }
        0
    };

    if maps.is_empty() && skipped_paks > 0 {
        bail!("no map assets found; all usable package indexes were unavailable");
    }

    for (map_id, asset_path) in maps {
        println!("{map_id}\t{asset_path}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::cooked_map_path;

    #[test]
    fn converts_cooked_umap_to_unreal_asset_path() {
        assert_eq!(
            cooked_map_path(
                "../../../DuneSandbox/Content/Dune/Maps/Story/Story_Zanovar_Start.umap"
            ),
            Some((
                "Story_Zanovar_Start".to_owned(),
                "/Game/Dune/Maps/Story/Story_Zanovar_Start.Story_Zanovar_Start".to_owned(),
            )),
        );
    }

    #[test]
    fn ignores_non_map_packages() {
        assert_eq!(
            cooked_map_path("../../../DuneSandbox/Content/Dune/Audio/Story_Zanovar_Start.uasset"),
            None,
        );
        assert_eq!(
            cooked_map_path("../../../DuneSandbox/Content/Dune/Maps/Story/NotAMap.uasset"),
            None,
        );
    }

    #[test]
    fn indexes_cooked_maps_outside_the_legacy_dune_maps_directory() {
        assert_eq!(
            cooked_map_path("../../../DuneSandbox/Content/Story/Story_Zanovar_Start.umap"),
            Some((
                "Story_Zanovar_Start".to_owned(),
                "/Game/Story/Story_Zanovar_Start.Story_Zanovar_Start".to_owned(),
            )),
        );
    }
}
