// Recompute every function's body from its code flow (Ghidra's CreateFunctionCmd.fixupFunctionBody).
// Repairs fragmented bodies left by creating functions late (e.g. table-only code). Prints changes.
//@category Ham
import ghidra.app.cmd.function.CreateFunctionCmd;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;

public class FixupBodies extends GhidraScript {
	@Override
	public void run() throws Exception {
		int changed = 0, total = 0;
		for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
			total++;
			long before = f.getBody().getNumAddresses();
			CreateFunctionCmd.fixupFunctionBody(currentProgram, f, monitor);
			long after = f.getBody().getNumAddresses();
			if (before != after) {
				changed++;
				println(String.format("%s %s: %d -> %d bytes", f.getEntryPoint(), f.getName(), before, after));
			}
		}
		println("bodies changed: " + changed + " of " + total);
	}
}
