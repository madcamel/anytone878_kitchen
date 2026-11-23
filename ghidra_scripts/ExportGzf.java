// Export the current program as a packed Ghidra file (.gzf), importable with analyzeHeadless -import.
// NOTE: a .gzf contains the full firmware image; share it only with people entitled to the firmware.
// args: <out.gzf>
//@category Ham
import ghidra.app.script.GhidraScript;
import java.io.File;

public class ExportGzf extends GhidraScript {
	@Override
	public void run() throws Exception {
		File out = new File(getScriptArgs()[0]);
		if (out.exists()) out.delete();
		currentProgram.getDomainFile().packFile(out, monitor);
		println("exported " + out + " (" + out.length() + " bytes)");
	}
}
