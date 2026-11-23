// Print the decompilation of the function at each address argument, using the program's own
// decompiler options (to compare against what the MCP backend shows).
//@category Ham
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.listing.Function;

public class DecompileAt extends GhidraScript {
	@Override
	public void run() throws Exception {
		DecompInterface di = new DecompInterface();
		DecompileOptions o = new DecompileOptions();
		o.grabFromProgram(currentProgram);
		di.setOptions(o);
		di.openProgram(currentProgram);
		println("respectReadOnly=" + o.isRespectReadOnly());
		for (String a : getScriptArgs()) {
			Function f = getFunctionAt(toAddr(Long.parseLong(a, 16)));
			DecompileResults r = di.decompileFunction(f, 60, monitor);
			println("\n" + r.getDecompiledFunction().getC());
		}
	}
}
